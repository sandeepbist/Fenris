"""Quota accounting for every Drive API call the client makes.

the build plan, quota reality - these unit costs drive ingest pacing:

- files.get (metadata read)       =   5 units
- files.list / changes.list        = 100 units
- downloads (alt=media)            = 200 units
- exports (files.export)           = 200 units  (Workspace-heavy corpora pay
  the download rate on every export - the pacing rule applies equally)

The point is not to predict Google's meter to the unit; it is to make the
cost of every decision visible: `status` prints it, eval's load/chaos suite
reports units/run, and a sync run that silently re-downloads a corpus shows
up as a units_used spike instead of a mystery.

Per-minute rate limiting needs no token bucket at MVP scale (initial ingest
paces downloads; 15-min polling is negligible); counters are enough, and the
real rate limiter is the backoff in errors.py reacting to 403/429s.
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field

__all__ = [
    "COST_DOWNLOAD",
    "COST_EXPORT",
    "COST_LIST_CHANGES",
    "COST_METADATA_READ",
    "QuotaCounter",
    "QuotaSnapshot",
]

# The cost table. Single source of truth; FakeDriveClient charges the same
# numbers so tests measure what production pays.
COST_METADATA_READ = 5      # files.get metadata
COST_LIST_CHANGES = 100     # files.list per page, changes.list per page,
                            # changes.getStartPageToken (metadata-class call)
COST_DOWNLOAD = 200         # files.get alt=media
COST_EXPORT = 200           # files.export

_UNIT_LABELS = {
    COST_METADATA_READ: "metadata_read",
    COST_LIST_CHANGES: "list_changes",
    COST_DOWNLOAD: "download",
    COST_EXPORT: "export",
}


@dataclass(frozen=True)
class QuotaSnapshot:
    """Immutable point-in-time view of the counters (for tests and `status`)."""

    units_used: int
    call_counts: dict[str, int]
    errors: dict[str, int]

    @property
    def total_calls(self) -> int:
        return sum(self.call_counts.values())


@dataclass
class QuotaCounter:
    """Thread-safe counters for Drive quota units and per-op call counts.

    Every DriveClient call routes through ``charge`` exactly once, AFTER the
    call succeeds (failed calls are recorded under ``count_error`` but do not
    consume units - Google does not bill a request the server rejected).
    """

    units_used: int = 0
    call_counts: Counter[str] = field(default_factory=Counter)
    error_counts: Counter[str] = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def charge(self, op: str, cost: int, calls: int = 1) -> None:
        """Record a successful API call and its quota units.

        ``op`` is a stable operation name ('list_files', 'get_metadata',
        'download_media', 'export', 'get_start_page_token', 'changes_list').
        """
        if cost not in _UNIT_LABELS:
            raise ValueError(
                f"unknown quota cost {cost!r}; use the COST_* constants so the "
                "cost table has exactly one home"
            )
        with self._lock:
            self.units_used += cost * calls
            self.call_counts[op] += calls

    def count_error(self, kind: str) -> None:
        """Record a failed call by error kind (rate_limit/permission/transient/other)."""
        with self._lock:
            self.error_counts[kind] += 1

    def snapshot(self) -> QuotaSnapshot:
        """Point-in-time copy; safe to hold while counters keep moving."""
        with self._lock:
            return QuotaSnapshot(
                units_used=self.units_used,
                call_counts=dict(self.call_counts),
                errors=dict(self.error_counts),
            )

    def reset(self) -> None:
        """Zero everything (test seam; also lets `status` show per-session numbers)."""
        with self._lock:
            self.units_used = 0
            self.call_counts.clear()
            self.error_counts.clear()
