"""The cursor drain: classify changes, stage network work, commit atomically.

Discipline (tested hard):

1. Classification first, from the change entry alone (no network):
   REMOVED / METADATA_ONLY / CONTENT.
2. Network work for CONTENT items (metadata fetch + download) runs
   OUTSIDE any transaction, staged by representation key.
3. One short transaction per page: retire superseded sources and mirror
   chunks_vec, apply ingest results, upsert drive_files, write the page
   token. The cursor never advances past uncommitted index state.
4. nextPageToken drains before newStartPageToken is committed.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from fenris.drive.errors import PermissionDeniedError
from fenris.ingest import router as router_mod
from fenris.ingest.pipeline import FileMeta
from fenris.sync.enumerate import DRIVE_ID_DEFAULT, _upsert_drive_file

REMOVED = "removed"
METADATA_ONLY = "metadata_only"
CONTENT = "content"


@dataclass
class SyncReport:
    drive_id: str = DRIVE_ID_DEFAULT
    pages_drained: int = 0
    changes_processed: int = 0
    metadata_only: int = 0
    content_reingests: int = 0
    tombstoned: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    resync_triggered: str = ""      # empty or the trigger reason
    errors: list[str] = field(default_factory=list)
    quota_snapshot: dict[str, Any] = field(default_factory=dict)


def _classify(entry: dict, local_version: str | None) -> str:
    if entry.get("removed"):
        return REMOVED
    if entry.get("file") is None:
        return METADATA_ONLY
    remote_meta = entry["file"]
    remote_version = str(remote_meta.get("version", ""))
    remote_md5 = remote_meta.get("md5Checksum") or ""
    if remote_version and local_version and remote_version == local_version:
        return METADATA_ONLY  # version unchanged -> nothing content-level moved
    if remote_md5 and not _local_md5_matches(conn=None, entry=entry):
        return CONTENT
    # No local representation at all (first sighting via changes):
    if local_version is None:
        return CONTENT
    # md5 changed vs what we indexed
    return CONTENT


def _local_md5_matches(conn, entry) -> bool:
    """Remote md5 vs the indexed source_revision for this file.

    conn may be None during classification for entries we cannot check;
    conservative answer is False (treat as content change). Callers that
    pass the real conn get the precise comparison below via
    _local_state (kept separate to keep classification pure).
    """
    return False


def _local_state(conn: sqlite3.Connection, drive_id: str, file_id: str) -> tuple[str | None, str | None]:
    """(drive_version on record, active source_revision) for a file."""
    row = conn.execute(
        """SELECT df.drive_version, s.source_revision
           FROM drive_files df
           LEFT JOIN sources s
             ON s.drive_id = df.drive_id AND s.file_id = df.file_id AND s.active = 1
           WHERE df.drive_id = ? AND df.file_id = ?""",
        (drive_id, file_id),
    ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _tombstone_file(conn: sqlite3.Connection, drive_id: str, file_id: str) -> int:
    """Retire every representation of a file + mirror chunks_vec.

    Returns the number of source rows tombstoned. Runs inside the page
    transaction - a removed file must stop serving even if its remote
    bytes are long gone.
    """
    cur = conn.execute(
        """SELECT id FROM sources WHERE drive_id = ? AND file_id = ?""",
        (drive_id, file_id),
    )
    n = 0
    for (source_id,) in cur.fetchall():
        conn.execute("UPDATE sources SET active = 0 WHERE id = ?", (source_id,))
        conn.execute(
            "UPDATE chunks_vec SET active = 0 "
            "WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_id = ?)",
            (source_id,),
        )
        n += 1
    # drive_files row stays (renames/moves of a removed file are still
    # observable) but marked trashed so staleness/reporting sees it.
    conn.execute(
        "UPDATE drive_files SET trashed = 1, updated_at = ? "
        "WHERE drive_id = ? AND file_id = ?",
        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), drive_id, file_id),
    )
    return n


def _file_meta_from(drive_id: str, meta: dict, resource_key: str = "") -> FileMeta:
    mime = meta.get("mimeType", "")
    return FileMeta(
        drive_id=drive_id,
        file_id=meta["id"],
        title=meta.get("name", ""),
        path="",
        mime_type=mime,
        modified_time=meta.get("modifiedTime", ""),
        source_revision=meta.get("md5Checksum") or "",
        drive_version=str(meta.get("version", "")),
        trashed=bool(meta.get("trashed")),
        is_workspace=mime.startswith("application/vnd.google-apps."),
        resource_key=resource_key,
    )


def _export_hash(client, file_id: str, target_mime: str) -> tuple[bytes, str]:
    """Workspace files have no content checksum: hash the exported bytes."""
    import hashlib

    data = client.export(file_id, target_mime)
    return data, "exp:" + hashlib.sha256(data).hexdigest()


def _fetch_content(conn, client, drive_id: str, meta: dict) -> tuple[FileMeta, bytes, str]:
    """Network path for one CONTENT item. Runs OUTSIDE any transaction.

    Returns (FileMeta with source_revision set, content bytes, status).
    Permission denials are swallowed into status - the caller marks the
    file unreadable and continues the page.
    """
    mime = meta.get("mimeType", "")
    resource_key = meta.get("resourceKey") or ""
    is_workspace = mime.startswith("application/vnd.google-apps.")
    decision = router_mod.route(mime, is_workspace, False)

    if decision.route == router_mod.Route.SHORTCUT:
        target_meta = client.get_metadata(meta.get("shortcutTargetId", ""))
        return _fetch_content(conn, client, drive_id, target_meta)

    try:
        if is_workspace:
            data, revision = _export_hash(client, meta["id"], decision.target_mime)
        else:
            data = client.download_media(meta["id"], resource_key)
            revision = meta.get("md5Checksum") or ""
    except PermissionDeniedError:
        fm = _file_meta_from(drive_id, meta, resource_key)
        fm_status = "unreadable"
        return fm, b"", fm_status

    fm = _file_meta_from(drive_id, meta, resource_key)
    fm = FileMeta(**{**fm.__dict__, "source_revision": revision}) if not fm.source_revision else fm
    if revision:
        # rebuild with the real revision (frozen dataclass)
        fm = FileMeta(
            drive_id=fm.drive_id, file_id=fm.file_id, title=fm.title, path=fm.path,
            mime_type=fm.mime_type, modified_time=fm.modified_time,
            source_revision=revision, drive_version=fm.drive_version,
            trashed=fm.trashed, auth_epoch=fm.auth_epoch,
            is_workspace=fm.is_workspace, is_shortcut=fm.is_shortcut,
            resource_key=fm.resource_key,
        )
    return fm, data, "ok"


def drain(conn: sqlite3.Connection, client, drive_id: str = DRIVE_ID_DEFAULT, *, cfg=None, pace_seconds: float | None = None, api_drive_id: str | None = None) -> SyncReport:
    """Walk the change feed from the saved cursor to the present.

    Per page: classify -> stage network work outside the transaction ->
    one short commit {index writes + page token}. A crash between pages
    replays the same page idempotently because every write is keyed.

    ``api_drive_id`` is Drive's driveId parameter (None = the
    authenticated account's My Drive - every vault's case; see
    enumerate_drive).
    """
    from fenris.sync.ingest_queue import IngestQueue

    report = SyncReport(drive_id=drive_id)

    row = conn.execute(
        "SELECT page_token, pending_start FROM sync_cursor WHERE drive_id = ?",
        (drive_id,),
    ).fetchone()
    if row is None:
        report.resync_triggered = "missing_cursor"
        return report

    token = row[0] or row[1]
    if not token:
        report.resync_triggered = "missing_cursor"
        return report

    queue = IngestQueue(pace_seconds=pace_seconds)
    page_token: str | None = token

    while page_token is not None:
        page = client.changes_list(page_token, api_drive_id)
        report.pages_drained += 1

        # --- classify + stage network work OUTSIDE the transaction -------
        staged: list[dict[str, Any]] = []
        for entry in page.changes:
            report.changes_processed += 1
            file_id = entry.get("fileId") or (entry.get("file") or {}).get("id")
            if not file_id:
                continue
            local_version, local_revision = _local_state(conn, drive_id, file_id)

            if entry.get("removed"):
                staged.append({"kind": REMOVED, "file_id": file_id})
                continue

            meta = entry.get("file")
            if meta is None:
                staged.append({"kind": METADATA_ONLY, "meta": {"id": file_id, "name": file_id}})
                continue

            # Trashed files stop serving: the representation is retired in
            # the same transaction (restore re-ingests it - md5 unchanged
            # means the drain treats restore as metadata until content moves).
            if meta.get("trashed"):
                staged.append({"kind": REMOVED, "file_id": file_id, "meta": meta})
                continue

            remote_md5 = meta.get("md5Checksum") or ""
            remote_version = str(meta.get("version", ""))
            content_changed = (
                local_revision is None
                or (remote_md5 and remote_md5 != local_revision)
                or (not remote_md5 and remote_version != (local_version or ""))
            )

            if not content_changed:
                staged.append({"kind": METADATA_ONLY, "meta": meta})
            else:
                fm, data, fetch_status = _fetch_content(conn, client, drive_id, meta)
                if fetch_status == "unreadable":
                    staged.append({"kind": "unreadable", "meta": meta})
                else:
                    result = queue.stage(fm, data, cfg)
                    staged.append({"kind": CONTENT, "result": result, "meta": meta})

        # --- ONE transaction: apply everything + advance the token -------
        conn.execute("BEGIN")
        try:
            for item in staged:
                kind = item["kind"]
                if kind == REMOVED:
                    report.tombstoned += _tombstone_file(conn, drive_id, item["file_id"])
                elif kind == "unreadable":
                    meta = item["meta"]
                    conn.execute(
                        """UPDATE sources SET status = 'unreadable', active = 0
                           WHERE drive_id = ? AND file_id = ?""",
                        (drive_id, meta["id"]),
                    )
                    conn.execute(
                        "UPDATE drive_files SET trashed = 1 WHERE drive_id = ? AND file_id = ?",
                        (drive_id, meta["id"]),
                    )
                    report.failed += 1
                elif kind == METADATA_ONLY:
                    _upsert_drive_file(conn, drive_id, item["meta"])
                    report.metadata_only += 1
                else:  # CONTENT
                    result = queue.apply(conn, item["result"])
                    _upsert_drive_file(conn, drive_id, item["meta"])
                    if result is None:
                        report.skipped += 1
                    elif result.status == "unchanged":
                        report.unchanged += 1
                    elif result.status in ("failed",):
                        report.failed += 1
                        report.errors.append(f"{result.source_id}: {result.reason}")
                    else:
                        report.content_reingests += 1
                        _mirror_vec_active(conn, drive_id, item["meta"]["id"])

            # Cursor discipline: the token advances ONLY with these writes.
            if page.next_page_token:
                conn.execute(
                    "UPDATE sync_cursor SET page_token = ?, status = 'ok', updated_at = ? "
                    "WHERE drive_id = ?",
                    (page.next_page_token, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), drive_id),
                )
                page_token = page.next_page_token
            else:
                final_token = page.new_start_page_token or page_token
                conn.execute(
                    "UPDATE sync_cursor SET page_token = ?, pending_start = ?, status = 'ok', updated_at = ? "
                    "WHERE drive_id = ?",
                    (
                        final_token,
                        page.new_start_page_token or row[1],
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        drive_id,
                    ),
                )
                page_token = None
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # Post-drain: staleness hints reflect current Drive reality (M4 seam).
    # Hint refresh is advisory: a failure here means staleness checks run
    # conservative, never that the sync itself failed.
    try:
        from fenris.sync.staleness import refresh_hints

        refresh_hints(conn, drive_id)
    except sqlite3.Error:
        pass

    report.quota_snapshot = client.quota.snapshot() if getattr(client, "quota", None) else {}
    return report


def _mirror_vec_active(conn: sqlite3.Connection, drive_id: str, file_id: str) -> None:
    """After a re-ingest, old representations' vec rows must be inactive.

    ingest_file retires superseded sources rows; this mirrors the
    retirement into chunks_vec for every non-active representation of
    the file so KNN stops serving the previous revision immediately.
    """
    cur = conn.execute(
        """SELECT c.chunk_id FROM chunks c
           JOIN sources s ON s.id = c.source_id
           WHERE s.drive_id = ? AND s.file_id = ? AND s.active = 0""",
        (drive_id, file_id),
    )
    ids = [r[0] for r in cur.fetchall()]
    if ids:
        conn.executemany(
            "UPDATE chunks_vec SET active = 0 WHERE chunk_id = ?", [(i,) for i in ids]
        )
