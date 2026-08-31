"""Sync: the correctness loop between Drive and the local projection.

The invariants this package enforces (they are the product's core
correctness contract):

- The change cursor NEVER advances past uncommitted index state: the
  page token is written in the same transaction as that page's index
  writes. A crash mid-drain replays the same page idempotently.
- All network work (metadata, download, export) happens OUTSIDE
  transactions. Commits are milliseconds, never seconds.
- Metadata-only changes (rename/move) upsert ``drive_files`` only -
  no download, no re-ingest, no new representation.
- ``removed=true`` tombstones every representation of the file even
  when the file is no longer fetchable (covers deletion AND loss of
  access from a share).
- One permission-denied file marks itself unreadable and the drain
  continues - a single forbidden response never aborts a sync.
"""

from fenris.sync.changes import SyncReport, drain
from fenris.sync.enumerate import enumerate_drive
from fenris.sync.ingest_queue import DOWNLOAD_PACE_SECONDS, IngestQueue
from fenris.sync.service import full_resync, sync_all, sync_status
from fenris.sync.staleness import (
    is_stale_sync,
    last_drain_at,
    refresh_hints,
)

__all__ = [
    "DOWNLOAD_PACE_SECONDS",
    "IngestQueue",
    "SyncReport",
    "drain",
    "enumerate_drive",
    "full_resync",
    "is_stale_sync",
    "last_drain_at",
    "refresh_hints",
    "sync_all",
    "sync_status",
]
