"""FakeDriveClient: the in-memory test double the whole offline suite uses.

Two layers, mirroring the real stack so client.py needs zero test-only code:

- ``FakeDriveService`` - stands in for the googleapiclient Resource: exposes
 ``files()`` / ``changes()`` builders returning request objects with a
 ``headers`` dict and ``execute()``, raising REAL ``googleapiclient.errors
.HttpError`` objects (built from real ``httplib2.Response`` dicts and real
 JSON bodies) so the client's classification/retry/backoff paths are
 exercised against the genuine exception types.
- ``FakeDriveClient`` - implements the exact same public contract as
 ``DriveClient`` (same method signatures, same pagination semantics, same
 quota cost table, same typed errors), driving FakeDriveService.

Faithfulness rules the tests rely on:
- pagination is deterministic (fixed page size, stable insertion order);
- quota is charged on the SAME cost table as the real client;
- injected failures are real HttpErrors with real reasons, so a
 ``fail_next_with(403, "rateLimitExceeded")`` goes through the same
 classify/backoff machinery, not a mock of it;
- mutation helpers (upload/edit/rename/move/trash/restore/unshare) exist so
 the freshness suite can author edit/rename/move/trash/restore scenarios
 offline (the build plan).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httplib2
from googleapiclient.errors import HttpError

from fenris.drive.client import (
    CHANGES_PAGE_SIZE,
    DEFAULT_PAGE_SIZE,
    LIST_FIELDS,
    ChangesPage,
    DriveClient,
)
from fenris.drive.quota import QuotaCounter

__all__ = ["FakeDriveClient", "FakeDriveService", "make_http_error"]


def make_http_error(
    status: int,
    reason: str,
    *,
    message: str | None = None,
    retry_after: str | None = None,
) -> HttpError:
    """Build a genuine googleapiclient HttpError, as Drive itself would.

 Real Drive bodies nest the machine reason under ``error.errors[].reason``
 while ``error.message`` carries prose - exactly the shape the classifier
 must see (the plain ``reason`` attribute of HttpError would otherwise be
 the message string).
 """
    response_info: dict[str, str] = {
        "status": str(status),
        "reason": {429: "Too Many Requests", 403: "Forbidden"}.get(status, "Error"),
    }
    if retry_after is not None:
        response_info["retry-after"] = retry_after
    resp = httplib2.Response(response_info)
    body = {
        "error": {
            "code": status,
            "message": message if message is not None else reason,
            "errors": [
                {
                    "domain": "usageLimits" if status in (403, 429) else "global",
                    "reason": reason,
                    "message": message if message is not None else reason,
                }
            ],
        }
    }
    return HttpError(resp, json.dumps(body).encode("utf-8"), uri="https://fake.drive")


class _FakeRequest:
    """A built-but-not-executed request: mirrors googleapiclient.http.HttpRequest
 closely enough that the client can attach headers before execute().

 The owning FakeDriveService records headers at execute time so tests can
 assert exactly what the request carried on the wire (e.g. the
 X-Goog-Drive-Resource-Keys header on media downloads).
 """

    def __init__(
        self,
        payload: Callable[[], Any],
        headers: dict[str, str] | None = None,
        *,
        recorder: Callable[[str, dict[str, str]], None] | None = None,
        op: str = "",
    ):
        self.headers: dict[str, str] = dict(headers or {})
        self._payload = payload
        self._recorder = recorder
        self._op = op

    def execute(self, http=None, num_retries=0):
        if self._recorder is not None:
            self._recorder(self._op, dict(self.headers))
        return self._payload()


class _FakeFilesResource:
    def __init__(self, service: FakeDriveService):
        self._svc = service

    def _req(self, op: str, payload: Callable[[], Any]) -> _FakeRequest:
        # Failure injection fires inside execute() - after headers were
        # attached, exactly like a real server rejecting the request.
        def body() -> Any:
            self._svc._check_failure(op)
            return payload()

        return _FakeRequest(body, recorder=self._svc._record_request, op=op)

    def list(self, **kwargs: Any) -> _FakeRequest:
        return self._req("files.list", lambda: self._svc._list_files_page(**kwargs))

    def get(self, **kwargs: Any) -> _FakeRequest:
        return self._req("files.get", lambda: self._svc._get_metadata(**kwargs))

    def get_media(self, **kwargs: Any) -> _FakeRequest:
        return self._req("files.get_media", lambda: self._svc._download(**kwargs))

    def export_media(self, **kwargs: Any) -> _FakeRequest:
        return self._req("files.export_media", lambda: self._svc._export(**kwargs))


class _FakeChangesResource:
    def __init__(self, service: FakeDriveService):
        self._svc = service

    def _req(self, op: str, payload: Callable[[], Any]) -> _FakeRequest:
        def body() -> Any:
            self._svc._check_failure(op)
            return payload()

        return _FakeRequest(body, recorder=self._svc._record_request, op=op)

    def getStartPageToken(self, **kwargs: Any) -> _FakeRequest:
        return self._req(
            "changes.getStartPageToken", lambda: self._svc._get_start_page_token(**kwargs)
        )

    def list(self, **kwargs: Any) -> _FakeRequest:
        return self._req("changes.list", lambda: self._svc._changes_page(**kwargs))


class FakeDriveService:
    """In-memory Drive v3: files dict + a change log with deterministic tokens.

 Page tokens are stable strings (``page-<n>`` for lists,
 ``start-<n>``/``change-cursor-<n>`` for changes) so tests can assert
 exact pagination behavior. The change log records every mutation so
 changes.list replays it Drive-style (entries carry fileId, removed flag,
 and - for live files - the same metadata fields as list_files).
 """

    def __init__(self, *, page_size: int = DEFAULT_PAGE_SIZE):
        self._files: dict[str, dict[str, Any]] = {}
        self._page_size = page_size
        self._next_change_seq = 1
        self._change_log: list[dict[str, Any]] = []
        # (operation, status, reason, retry_after) raised ONCE on next match
        self._failures: list[tuple[str, int, str, str | None]] = []
        # every executed request: {"op": str, "headers": dict} - header assertions
        self.requests: list[dict[str, Any]] = []

    # ------------------------------------------------------------ drive shape

    def files(self) -> _FakeFilesResource:
        return _FakeFilesResource(self)

    def changes(self) -> _FakeChangesResource:
        return _FakeChangesResource(self)

    def _record_request(self, op: str, headers: dict[str, str]) -> None:
        self.requests.append({"op": op, "headers": headers})

    def fail_next_with(
        self, status: int, reason: str, *, op: str | None = None, retry_after: str | None = None
    ) -> None:
        """Make the NEXT matching API call raise a real HttpError once.

 ``op`` narrows the failure to one endpoint ('files.list',
 'files.get', 'files.get_media', 'files.export_media',
 'changes.getStartPageToken', 'changes.list'); None means any.
 """
        self._failures.append((op or "*", status, reason, retry_after))

    def _check_failure(self, op: str) -> None:
        for i, (target, status, reason, retry_after) in enumerate(self._failures):
            if target in ("*", op):
                del self._failures[i]
                raise make_http_error(
                    status, reason, retry_after=retry_after, message=f"{reason} (injected)"
                )

    # ------------------------------------------------------- mutation helpers

    def upload(
        self,
        file_id: str,
        *,
        name: str = "untitled",
        mime_type: str = "application/octet-stream",
        content: bytes = b"",
        md5_checksum: str = "",
        parents: list[str] | None = None,
        resource_key: str | None = None,
        can_download: bool = True,
        modified_time: str = "2026-01-01T00:00:00.000Z",
    ) -> dict[str, Any]:
        """Create a file entry. Returns the stored metadata dict."""
        record = {
            "id": file_id,
            "name": name,
            "mimeType": mime_type,
            "modifiedTime": modified_time,
            "md5Checksum": md5_checksum,
            "version": "1",
            "trashed": False,
            "parents": parents or ["root"],
            "resourceKey": resource_key,
            "capabilities": {"canDownload": can_download},
            "size": str(len(content)),
            "_content": content,
        }
        self._files[file_id] = record
        self._record_change(file_id, removed=False)
        return self._public_view(record)

    def edit(self, file_id: str, content: bytes, *, md5_checksum: str, version: str) -> None:
        """Content change: bumps version, updates checksum, logs a change."""
        rec = self._require(file_id)
        rec["_content"] = content
        rec["md5Checksum"] = md5_checksum
        rec["version"] = version
        rec["size"] = str(len(content))
        self._record_change(file_id, removed=False)

    def rename(self, file_id: str, name: str) -> None:
        """Metadata-only change: bump version, SAME md5 (the build-plan rename rule:
 a rename bumps Drive's version without a content change)."""
        rec = self._require(file_id)
        rec["name"] = name
        rec["version"] = str(int(rec["version"]) + 1)
        self._record_change(file_id, removed=False)

    def move(self, file_id: str, parents: list[str]) -> None:
        """Metadata-only change: new parent set, same md5."""
        rec = self._require(file_id)
        rec["parents"] = list(parents)
        rec["version"] = str(int(rec["version"]) + 1)
        self._record_change(file_id, removed=False)

    def trash(self, file_id: str) -> None:
        rec = self._require(file_id)
        rec["trashed"] = True
        rec["version"] = str(int(rec["version"]) + 1)
        self._record_change(file_id, removed=False)

    def restore(self, file_id: str) -> None:
        rec = self._require(file_id)
        rec["trashed"] = False
        rec["version"] = str(int(rec["version"]) + 1)
        self._record_change(file_id, removed=False)

    def unshare(self, file_id: str) -> None:
        """Loss of access: Drive emits removed=true; the file leaves our view.

 the build plan: tombstone regardless of fetchability - deletion and
 loss-of-access both surface as removed.
 """
        self._files.pop(file_id, None)
        self._record_change(file_id, removed=True)

    def remove(self, file_id: str) -> None:
        """Hard delete (alias for the deletion flavor of unshare)."""
        self.unshare(file_id)

    # ------------------------------------------------------------ internals

    def _require(self, file_id: str) -> dict[str, Any]:
        try:
            return self._files[file_id]
        except KeyError:
            raise make_http_error(404, "notFound", message=f"File not found: {file_id}") from None

    def _public_view(self, rec: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in rec.items() if not k.startswith("_")}

    def _record_change(self, file_id: str, *, removed: bool) -> int:
        seq = self._next_change_seq
        self._next_change_seq += 1
        entry: dict[str, Any] = {"changeId": seq, "fileId": file_id, "removed": removed}
        if not removed and file_id in self._files:
            entry["file"] = self._public_view(self._files[file_id])
        self._change_log.append(entry)
        return seq

    # ------------------------------------------------------- endpoint bodies

    def _list_files_page(self, *, pageToken=None, pageSize=None, fields=None, **kw: Any):
        # Deterministic order: insertion order of the dict.
        items = [self._public_view(r) for r in self._files.values()]
        size = int(pageSize or self._page_size or DEFAULT_PAGE_SIZE)
        start = 0
        if pageToken:
            try:
                start = int(str(pageToken).split("-", 1)[1])
            except (IndexError, ValueError) as e:
                raise make_http_error(400, "invalidPageToken", message=str(e)) from None
        window = items[start : start + size]
        next_token = f"page-{start + size}" if start + size < len(items) else None
        return {"files": window, "nextPageToken": next_token}

    def _get_metadata(self, *, fileId, fields=None, resourceKey=None, **kw: Any):
        rec = self._files.get(fileId)
        if rec is None:
            raise make_http_error(404, "notFound", message=f"File not found: {fileId}")
        return self._public_view(rec)

    def _download(self, *, fileId, headers=None, **kw: Any):
        rec = self._files.get(fileId)
        if rec is None:
            raise make_http_error(404, "notFound", message=f"File not found: {fileId}")
        return bytes(rec["_content"])

    def _export(self, *, fileId, mimeType, headers=None, **kw: Any):
        rec = self._files.get(fileId)
        if rec is None:
            raise make_http_error(404, "notFound", message=f"File not found: {fileId}")
        # Export target bytes are derived deterministically so 'exp:'+sha256
        # identity (the build plan) is reproducible in tests.
        return bytes(rec["_content"])

    def _get_start_page_token(self, *, driveId=None, **kw: Any):
        # Token = current change-log length: replaying from it yields exactly
        # the changes logged AFTER this call (Drive's own semantics).
        return {"startPageToken": f"start-{len(self._change_log)}"}

    def _changes_page(self, *, pageToken, pageSize=None, **kw: Any):
        size = int(pageSize or CHANGES_PAGE_SIZE)
        try:
            token = str(pageToken)
            start = int(token.split("-", 1)[1]) if "-" in token else int(token)
        except (IndexError, ValueError) as e:
            raise make_http_error(400, "invalidPageToken", message=str(e)) from None
        window = self._change_log[start : start + size]
        # Page tokens carry the absolute consumed-count, so multi-page walks
        # share one format whether they started at 'start-N' or 'change-N'.
        next_token = (
            f"change-{start + size}" if start + size < len(self._change_log) else None
        )
        result: dict[str, Any] = {"changes": [dict(c) for c in window]}
        if next_token:
            result["nextPageToken"] = next_token
        else:
            result["newStartPageToken"] = f"start-{len(self._change_log)}"
        return result


class FakeDriveClient:
    """Same public contract as DriveClient, but driving FakeDriveService.

 Implemented as a thin subclass-free adapter: it owns a FakeDriveService
 and a REAL DriveClient (so pagination, quota accounting, classification,
 backoff, and the canDownload gate are the production code paths, never a
 re-implementation), plus the mutation helpers tests author scenarios with.
 The fake deliberately subclasses nothing from DriveClient's public face -
 it IS one, compositionally, which is the whole point: the test double
 cannot drift from the real contract.
 """

    def __init__(
        self,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        changes_page_size: int = CHANGES_PAGE_SIZE,
        quota: QuotaCounter | None = None,
        sleep: Callable[[float], None] = lambda _s: None,
        rng: Callable[[], float] | None = None,
    ):
        self.service = FakeDriveService(page_size=page_size)
        self.quota = quota if quota is not None else QuotaCounter()
        self._page_size = page_size
        self._changes_page_size = changes_page_size
        self._inner = DriveClient(
            self.service,
            quota=self.quota,
            sleep=sleep,
            rng=rng,
            changes_page_size=changes_page_size,
        )

    # ----------------------------------------------------- DriveClient surface

    def list_files(
        self, page_token: str | None = None, *, fields: str = LIST_FIELDS,
        page_size: int | None = None, drive_id: str | None = None,
        query: str | None = None,
    ):
        """``page_size`` defaults to the constructor's, so pagination tests are
 deterministic without each caller re-stating it."""
        return self._inner.list_files(
            page_token, fields=fields,
            page_size=page_size if page_size is not None else self._page_size,
            drive_id=drive_id, query=query,
        )

    def get_metadata(self, file_id: str, resource_key: str | None = None):
        return self._inner.get_metadata(file_id, resource_key)

    def download_media(
        self, file_id: str, resource_key: str | None = None, *, acknowledge_abuse: bool = False
    ) -> bytes:
        return self._inner.download_media(
            file_id, resource_key, acknowledge_abuse=acknowledge_abuse
        )

    def export(self, file_id: str, mime: str) -> bytes:
        return self._inner.export(file_id, mime)

    def get_start_page_token(self, drive_id: str | None = None) -> str:
        return self._inner.get_start_page_token(drive_id)

    def changes_list(self, page_token: str, drive_id: str | None = None) -> ChangesPage:
        return self._inner.changes_list(page_token, drive_id)

    def retry_count(self) -> int:
        return self._inner.retry_count()

    @property
    def quota_snapshot(self):
        return self.quota.snapshot()

    # ------------------------------------------------------ mutation helpers

    def upload(self, file_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.service.upload(file_id, **kwargs)

    def edit(self, file_id: str, content: bytes, *, md5_checksum: str, version: str) -> None:
        self.service.edit(file_id, content, md5_checksum=md5_checksum, version=version)

    def rename(self, file_id: str, name: str) -> None:
        self.service.rename(file_id, name)

    def move(self, file_id: str, parents: list[str]) -> None:
        self.service.move(file_id, parents)

    def trash(self, file_id: str) -> None:
        self.service.trash(file_id)

    def restore(self, file_id: str) -> None:
        self.service.restore(file_id)

    def unshare(self, file_id: str) -> None:
        self.service.unshare(file_id)

    def fail_next_with(
        self, status: int, reason: str, *, op: str | None = None, retry_after: str | None = None
    ) -> None:
        self.service.fail_next_with(status, reason, op=op, retry_after=retry_after)
