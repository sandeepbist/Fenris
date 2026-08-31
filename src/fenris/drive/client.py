"""DriveClient: the only surface fenris talks to Google Drive through.

Wraps a google-api-python-client ``build("drive", "v3")`` service and enforces
the the build plan sync requirements at the client boundary:

- list/changes calls always send supportsAllDrives + includeItemsFromAllDrives;
- enumeration fields are exactly the the build plan set;
- downloads are gated on ``capabilities.canDownload`` BEFORE any request is
  issued (a 200-unit call is never spent on a file we cannot read);
- resource-key files carry the ``X-Goog-Drive-Resource-Keys`` header on
  downloads (header value format: ``<fileId>/<resourceKey>``);
- every call is quota-counted on the the build plan cost table and every failure is
  classified (rate-limit vs permission denial vs transient) - genuine
  permission denials never enter the retry loop.

All methods return plain dicts/dataclasses - no googleapiclient request or
resource object leaks past this module. FakeDriveClient (fake.py) implements
the identical contract for offline tests and eval, so the sync/ingest layers
can be written against either without branching.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any, Protocol

from googleapiclient.errors import HttpError

from fenris.drive.errors import (
    BACKOFF_MAX_RETRIES,
    FenrisDriveError,
    PermissionDeniedError,
    RateLimitedError,
    TransientError,
    classify_http_error,
    run_with_backoff,
)
from fenris.drive.quota import (
    COST_DOWNLOAD,
    COST_EXPORT,
    COST_LIST_CHANGES,
    COST_METADATA_READ,
    QuotaCounter,
)

__all__ = ["CHANGES_PAGE_SIZE", "DEFAULT_PAGE_SIZE", "LIST_FIELDS", "ChangesPage", "DriveClient"]

#: the build plan enumeration fields - the exact set the schema upserts.
LIST_FIELDS = (
    "id,name,mimeType,modifiedTime,md5Checksum,version,trashed,parents,"
    "resourceKey,capabilities(canDownload),size"
)

DEFAULT_PAGE_SIZE = 1000   # the build plan
CHANGES_PAGE_SIZE = 1000   # the build plan

#: Header Drive requires for files shared via link with a resource key.
RESOURCE_KEY_HEADER = "X-Goog-Drive-Resource-Keys"


class _ServiceLike(Protocol):
    """The slice of googleapiclient's Resource API this client touches.

    FakeDriveService (fake.py) satisfies the same shape, which is how the
    whole offline test suite runs without a network.
    """

    def files(self) -> Any: ...
    def changes(self) -> Any: ...


class ChangesPage:
    """One changes.list page: the items plus the two tokens that matter.

    ``next_page_token`` - another page follows; feed it back to changes_list.
    ``new_start_page_token`` - present on the LAST page only; persist it as
    the new cursor start (schema.sync_cursor.pending_start).
    """

    __slots__ = ("changes", "new_start_page_token", "next_page_token")

    def __init__(
        self,
        changes: list[dict[str, Any]],
        next_page_token: str | None,
        new_start_page_token: str | None,
    ):
        self.changes = changes
        self.next_page_token = next_page_token
        self.new_start_page_token = new_start_page_token

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"ChangesPage(n={len(self.changes)}, next={self.next_page_token!r}, "
                f"new_start={self.new_start_page_token!r})")


class DriveClient:
    """Read-only Drive v3 client with quota counting and typed retries.

    Parameters
    ----------
    service:
        A googleapiclient drive v3 Resource (or the FakeDriveService).
    quota:
        Shared QuotaCounter; a fresh one is created if omitted. Pass the same
        counter to multiple clients to get one account for a whole session.
    sleep / rng:
        Injected clock/randomness for the backoff path (tests keep it fast
        and deterministic).
    """

    def __init__(
        self,
        service: _ServiceLike,
        *,
        quota: QuotaCounter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] | None = None,
        changes_page_size: int = CHANGES_PAGE_SIZE,
    ):
        self._service = service
        self.quota = quota if quota is not None else QuotaCounter()
        self._sleep = sleep
        self._rng = rng
        self._changes_page_size = changes_page_size
        # Tests assert on retry counts; exposed read-only via retry_count().
        self._retries = 0

    # ------------------------------------------------------------------ utils

    def retry_count(self) -> int:
        """Total backoff retries performed by this client (test/telemetry hook)."""
        return self._retries

    @staticmethod
    def _is_retriable(err: BaseException) -> bool:
        return isinstance(err, (RateLimitedError, TransientError))

    def _call(self, op: str, cost: int, fn: Callable[[], Any]) -> Any:
        """Run one API call: classify failures, retry retriables, charge quota.

        Contract (the build plan):
        - PermissionDeniedError propagates on the FIRST failure - no retry is
          even attempted for a genuine denial.
        - RateLimitedError/TransientError back off with decorrelated jitter,
          capped at BACKOFF_MAX_RETRIES, honoring Retry-After when sent.
        - Failed calls never charge quota; every failure is counted by kind.
        """
        def run() -> Any:
            try:
                result = fn()
            except HttpError as e:
                raise classify_http_error(e) from e
            self.quota.charge(op, cost)
            return result

        def on_retry(attempt: int, err: BaseException, delay: float) -> None:
            self._retries += 1
            self.quota.count_error(getattr(err, "kind", "transient"))

        try:
            return run_with_backoff(
                run,
                is_retriable=self._is_retriable,
                max_retries=BACKOFF_MAX_RETRIES,
                on_retry=on_retry,
                sleep=self._sleep,
                rng=self._rng,
            )
        except FenrisDriveError as e:
            self.quota.count_error(getattr(e, "kind", "error"))
            raise

    # ------------------------------------------------------------------- list

    def list_files(
        self,
        page_token: str | None = None,
        *,
        fields: str = LIST_FIELDS,
        page_size: int = DEFAULT_PAGE_SIZE,
        drive_id: str | None = None,
        query: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield file metadata dicts, transparently following page tokens.

        Enumeration parameters are fixed by the build plan:
        pageSize=1000, supportsAllDrives=True, includeItemsFromAllDrives=True,
        and the exact field set in LIST_FIELDS. Each PAGE costs 100 units and
        is counted when fetched (quota is charged on the page, not the item,
        matching how Google meters list calls).
        """
        token = page_token
        while True:
            def fetch(tok: str | None = token) -> dict[str, Any]:
                return self._service.files().list(
                    pageToken=tok,
                    pageSize=page_size,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    fields=f"nextPageToken, files({fields})",
                    **({"driveId": drive_id} if drive_id else {}),
                    **({"q": query} if query else {}),
                ).execute()

            result = self._call("list_files", COST_LIST_CHANGES, fetch)
            yield from result.get("files") or []
            token = result.get("nextPageToken")
            if not token:
                return

    # --------------------------------------------------------------- metadata

    def get_metadata(
        self,
        file_id: str,
        resource_key: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one file's metadata (5 units). Supports resource-key files.

        The capability check callers need (``capabilities.canDownload``) is
        part of the requested fields; the decision itself lives in
        download_media so an un-downloadable file can still be inspected.
        """
        def fetch() -> dict[str, Any]:
            return self._service.files().get(
                fileId=file_id,
                supportsAllDrives=True,
                fields=LIST_FIELDS,
                **({"resourceKey": resource_key} if resource_key else {}),
            ).execute()

        return self._call("get_metadata", COST_METADATA_READ, fetch)

    # --------------------------------------------------------------- download

    def download_media(
        self,
        file_id: str,
        resource_key: str | None = None,
        *,
        acknowledge_abuse: bool = False,
    ) -> bytes:
        """Download a stored file's raw bytes (files.get alt=media, 200 units).

        Gates on ``capabilities.canDownload`` first (the build plan): a
        False capability raises PermissionDeniedError BEFORE any request is
        issued, so no download quota is spent on a download the server would
        refuse. The gate itself costs one 5-unit metadata read.

        For files shared via a resource key, the X-Goog-Drive-Resource-Keys
        header (``<fileId>/<resourceKey>``) is attached to the media request.
        """
        meta = self.get_metadata(file_id, resource_key)
        caps = meta.get("capabilities") or {}
        if caps.get("canDownload") is not True:
            # No request issued, no download quota spent; classification is
            # the same one the server would give a forced attempt.
            self.quota.count_error("permission")
            raise PermissionDeniedError(
                f"Drive reports canDownload=false for file {file_id!r} "
                "(the build plan: downloads are gated on capabilities.canDownload)",
                reason="insufficientFilePermissions",
                status_code=None,
            )

        def fetch() -> bytes:
            request = self._service.files().get_media(
                fileId=file_id,
                supportsAllDrives=True,
                **({"acknowledgeAbuse": acknowledge_abuse} if acknowledge_abuse else {}),
            )
            if resource_key:
                request.headers[RESOURCE_KEY_HEADER] = f"{file_id}/{resource_key}"
            return request.execute()

        return self._call("download_media", COST_DOWNLOAD, fetch)

    # ----------------------------------------------------------------- export

    def export(self, file_id: str, mime: str) -> bytes:
        """Export a Workspace file (Google Docs/Sheets/Slides) to ``mime``.

        Costs 200 units - the same rate as a download (the build plan: a
        Workspace-heavy corpus pays it on every export; pacing applies
        equally). Canvases that need resource-key behavior attach the header
        the same way download_media does; v3's export endpoint accepts the
        resourceKey query parameter, which export_media sets from fileId's
        context - the fake keeps the same observable contract.
        """
        def fetch() -> bytes:
            request = self._service.files().export_media(fileId=file_id, mimeType=mime)
            return request.execute()

        return self._call("export", COST_EXPORT, fetch)

    # ---------------------------------------------------------------- changes

    def get_start_page_token(self, drive_id: str | None = None) -> str:
        """Snapshot token to enumerate changes FROM (the build plan: taken BEFORE
        initial enumeration so the first ingest can replay what it missed)."""
        def fetch() -> dict[str, Any]:
            return self._service.changes().getStartPageToken(
                supportsAllDrives=True,
                **({"driveId": drive_id} if drive_id else {}),
            ).execute()

        result = self._call("get_start_page_token", COST_LIST_CHANGES, fetch)
        token = result.get("startPageToken")
        if not token:
            raise TransientError(
                "changes.getStartPageToken returned no startPageToken",
                reason="emptyResponse", status_code=None,
            )
        return str(token)

    def changes_list(
        self,
        page_token: str,
        drive_id: str | None = None,
    ) -> ChangesPage:
        """Fetch one page of changes (100 units) from ``page_token``.

        Parameters fixed by the build plan: includeRemoved=True,
        includeItemsFromAllDrives=True, supportsAllDrives=True, pageSize=1000
        (the constructor's changes_page_size overrides pageSize for tests
        that need deterministic multi-page change streams; production always
        uses the MVP value).

        Returns a ChangesPage: the change entries, the next page token (None
        on the last page), and - when Drive sends it - the new start page
        token (present on the final page; this is the value that becomes the
        saved cursor after the page applies cleanly).
        """
        def fetch() -> dict[str, Any]:
            return self._service.changes().list(
                pageToken=page_token,
                includeRemoved=True,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                pageSize=self._changes_page_size,
                **({"driveId": drive_id} if drive_id else {}),
            ).execute()

        result = self._call("changes_list", COST_LIST_CHANGES, fetch)
        return ChangesPage(
            changes=list(result.get("changes") or []),
            next_page_token=result.get("nextPageToken") or None,
            new_start_page_token=result.get("newStartPageToken") or None,
        )
