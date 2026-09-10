"""M1 Drive-slice tests - fully offline.

FakeDriveClient drives the REAL DriveClient code paths (pagination, quota
charging, classification, backoff, the canDownload gate) against an in-memory
service that raises genuine googleapiclient HttpErrors built from real
httplib2.Response objects - so the retry/denial paths are exercised against
the actual exception types, not mocks of them. Auth tests inject fake
keyring backends and a fake network POST; nothing here touches the network.
"""

from __future__ import annotations

import io
import json

import pytest
from typer.testing import CliRunner

import fenris.drive.auth as auth_mod
from fenris.drive.auth import (
    KEYRING_ACCOUNT,
    KEYRING_SERVICE,
    REVOKE_URL,
    AuthError,
    KeyringUnavailableError,
)
from fenris.drive.client import LIST_FIELDS, DriveClient
from fenris.drive.errors import (
    BACKOFF_MAX_RETRIES,
    PermissionDeniedError,
    RateLimitedError,
    TransientError,
    backoff,
    classify_http_error,
)
from fenris.drive.fake import FakeDriveClient, make_http_error
from fenris.drive.quota import (
    COST_DOWNLOAD,
    COST_EXPORT,
    COST_LIST_CHANGES,
    COST_METADATA_READ,
    QuotaCounter,
)

DUMMY_TOKEN_FOR_TESTS = "obviously-fake-test-token"

# --------------------------------------------------------------------- helpers


def make_client(**kwargs) -> FakeDriveClient:
    """A fake with deterministic pagination and instant (no-op) backoff sleep."""
    return FakeDriveClient(**kwargs)


class _RecordingSleep:
    """Sleep stub that records delays instead of blocking."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class FakeKeyring:
    """In-memory keyring stand-in: get/delete only (what disconnect uses)."""

    def __init__(self, stored: str | None = "token-abc"):
        self.stored = stored
        self.deleted: list[tuple[str, str]] = []

    def get_password(self, service: str, account: str) -> str | None:
        return self.stored

    def delete_password(self, service: str, account: str) -> None:
        self.deleted.append((service, account))
        self.stored = None


class FakeOpener:
    """urllib opener stand-in: records the revoke POST, serves a status."""

    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list[object] = []

    def open(self, request, timeout=None):
        self.requests.append(request)

        class _Resp(io.BytesIO):
            status = self.status

        return _Resp(b"")


# ------------------------------------------------------------ pagination (list)


def test_list_files_paginates_across_pages():
    """A 10-file corpus with page_size=4 walks pages and yields all 10 once."""
    c = make_client(page_size=4)
    for i in range(10):
        c.upload(f"f{i}", name=f"file-{i}")

    got = [f["id"] for f in c.list_files()]
    assert got == [f"f{i}" for i in range(10)]
    # 10 files / 4 per page = 3 pages, each costing 100 units.
    snap = c.quota.snapshot()
    assert snap.call_counts["list_files"] == 3
    assert snap.units_used == 3 * COST_LIST_CHANGES


def test_list_files_page_tokens_are_resume_points():
    """The generator's token is a resumable cursor: re-entering from it yields
 exactly the remaining files (this is what a killed ingest relies on)."""
    c = make_client(page_size=3)
    for i in range(7):
        c.upload(f"f{i}")

    it = c.list_files()
    first = [next(it)["id"] for _ in range(3)]  # consume page 1 only
    assert first == ["f0", "f1", "f2"]

    # A fresh generator started from page 2's token yields the rest.
    rest = [f["id"] for f in c.list_files(page_token="page-3")]
    assert rest == [f"f{i}" for i in range(3, 7)]


def test_list_files_fields_are_the_spec_set():
    """The field string must be exactly the build plan's enumeration set."""
    assert LIST_FIELDS == (
        "id,name,mimeType,modifiedTime,md5Checksum,version,trashed,parents,"
        "resourceKey,capabilities(canDownload),size"
    )


def test_list_files_metadata_shape_round_trips_the_schema_columns():
    """Everything drive_files/sources need must be present on each item."""
    c = make_client()
    c.upload("doc1", name="Q3 plan", mime_type="application/pdf",
             content=b"pdfbytes", md5_checksum="md5q3", resource_key="rk-doc1")
    item = next(iter(c.list_files()))
    assert item["id"] == "doc1"
    assert item["md5Checksum"] == "md5q3"
    assert item["mimeType"] == "application/pdf"
    assert item["trashed"] is False
    assert item["parents"] == ["root"]
    assert item["resourceKey"] == "rk-doc1"
    assert item["capabilities"]["canDownload"] is True
    assert item["size"] == "8"


# --------------------------------------------------------------- quota counting


def test_quota_table_matches_mvp_costs_exactly():
    """metadata=5, list/changes=100, download=200, export=200 (the build plan)."""
    assert COST_METADATA_READ == 5
    assert COST_LIST_CHANGES == 100
    assert COST_DOWNLOAD == 200
    assert COST_EXPORT == 200


def test_quota_charged_per_call_not_per_item():
    """A list page of N items costs 100 units once - quota rides the request."""
    c = make_client()
    for i in range(50):
        c.upload(f"f{i}")
    n = sum(1 for _ in c.list_files())
    assert n == 50
    assert c.quota.units_used == COST_LIST_CHANGES  # not 50 x anything


def test_quota_counts_each_operation_on_the_cost_table():
    """Every op family lands on its own counter bucket with the right units."""
    c = make_client()
    c.upload("a", content=b"x")
    c.upload("doc", mime_type="application/vnd.google-apps.document", content=b"docbytes")
    spt = c.get_start_page_token()          # 100 (changes-class call)
    c.get_metadata("a")                      # 5
    c.download_media("a")                    # 5 (gate read) + 200
    c.export("doc", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")  # 200
    c.changes_list(spt)                      # 100

    snap = c.quota.snapshot()
    assert snap.units_used == 100 + 5 + 5 + 200 + 200 + 100
    assert snap.call_counts == {
        "get_start_page_token": 1,
        "get_metadata": 2,      # one direct, one for download_media's canDownload gate
        "download_media": 1,
        "export": 1,
        "changes_list": 1,
    }


def test_failed_calls_do_not_charge_quota_but_do_count_errors():
    """A rejected request costs nothing on Drive's meter; we mirror that and
 record the failure by kind so `status` can show it."""
    c = make_client()
    c.upload("a")
    c.fail_next_with(403, "rateLimitExceeded", op="files.get")
    c.get_metadata("a")  # one failed attempt, then success
    snap = c.quota.snapshot()
    assert snap.units_used == COST_METADATA_READ
    assert snap.errors == {"rate_limit": 1}


def test_quota_snapshot_is_a_copy_and_reset_clears():
    q = QuotaCounter()
    q.charge("get_metadata", COST_METADATA_READ)
    snap = q.snapshot()
    q.charge("get_metadata", COST_METADATA_READ)
    assert snap.units_used == 5            # snapshot frozen
    assert q.units_used == 10
    q.reset()
    assert q.units_used == 0
    assert q.snapshot().call_counts == {}


def test_unknown_quota_cost_is_rejected():
    """A cost outside the table is a programming error, not a silent number."""
    q = QuotaCounter()
    with pytest.raises(ValueError):
        q.charge("something", 42)


# --------------------------------------------------------- 403/429 reason split


def _http_error(status, reason, retry_after=None):
    return make_http_error(status, reason, retry_after=retry_after)


def test_classify_rate_limited_403_is_retryable():
    err = _http_error(403, "rateLimitExceeded")
    out = classify_http_error(err)
    assert isinstance(out, RateLimitedError)
    assert out.retryable is True
    assert out.reason == "rateLimitExceeded"
    assert out.status_code == 403


@pytest.mark.parametrize("reason", ["dailyLimitExceeded", "userRateLimitExceeded", "tooManyAttempts"])
def test_classify_quota_reasons_all_rate_limited(reason):
    if reason == "tooManyAttempts":  # the 429 status family
        out = classify_http_error(_http_error(429, reason))
    else:
        out = classify_http_error(_http_error(403, reason))
    assert isinstance(out, RateLimitedError)


def test_classify_429_without_reason_still_rate_limited():
    """Bare 429 with an unrecognized body reason falls back to status-code
 semantics: retryable."""
    out = classify_http_error(_http_error(429, "someOpaqueReason"))
    assert isinstance(out, RateLimitedError)
    assert out.retry_after is None


def test_classify_forbidden_is_never_retryable():
    out = classify_http_error(_http_error(403, "forbidden"))
    assert isinstance(out, PermissionDeniedError)
    assert out.retryable is False


def test_classify_insufficient_permissions_is_never_retryable():
    out = classify_http_error(_http_error(403, "insufficientFilePermissions"))
    assert isinstance(out, PermissionDeniedError)


def test_classify_5xx_is_transient():
    out = classify_http_error(_http_error(500, "backendError"))
    assert isinstance(out, TransientError)
    assert out.retryable is True
    out2 = classify_http_error(_http_error(503, "serviceUnavailable"))
    assert isinstance(out2, TransientError)


def test_classify_unknown_4xx_is_not_retryable_and_not_a_permission_claim():
    out = classify_http_error(_http_error(400, "invalidPageToken"))
    assert not isinstance(out, (RateLimitedError, TransientError))
    assert not isinstance(out, PermissionDeniedError)  # never fake a denial


# ------------------------------------------------------------------- retry path


def test_rate_limit_is_retried_with_backoff_and_then_succeeds():
    """403 rateLimitExceeded: backoff retries, call succeeds, retry counted."""
    sleeper = _RecordingSleep()
    c = FakeDriveClient(sleep=sleeper)
    c.upload("a")
    c.fail_next_with(403, "rateLimitExceeded", op="files.get")
    meta = c.get_metadata("a")
    assert meta["id"] == "a"
    assert c.retry_count() == 1
    assert len(sleeper.delays) == 1          # exactly one backoff sleep
    assert sleeper.delays[0] > 0


def test_rate_limit_exhausts_retries_then_raises():
    """Persistent rate limiting stops at the MVP cap (5) and surfaces the error."""
    sleeper = _RecordingSleep()
    c = FakeDriveClient(sleep=sleeper)
    c.upload("a")
    for _ in range(BACKOFF_MAX_RETRIES + 2):
        c.fail_next_with(403, "rateLimitExceeded", op="files.get")
    with pytest.raises(RateLimitedError):
        c.get_metadata("a")
    assert c.retry_count() == BACKOFF_MAX_RETRIES
    assert len(sleeper.delays) == BACKOFF_MAX_RETRIES


def test_permission_denial_raises_immediately_no_retry_attempted():
    """THE the build plan rule: genuine denial never enters the retry loop. Assert the
 retry counter did not move and no backoff sleep was scheduled."""
    sleeper = _RecordingSleep()
    c = FakeDriveClient(sleep=sleeper)
    c.upload("a")
    c.fail_next_with(403, "insufficientFilePermissions", op="files.get")
    with pytest.raises(PermissionDeniedError):
        c.get_metadata("a")
    assert c.retry_count() == 0
    assert sleeper.delays == []          # no backoff computed, let alone slept


def test_denial_inside_backoff_sequence_stops_immediately():
    """A denial arriving mid-sequence (attempt 2 of a rate-limited stretch)
 still aborts instantly rather than burning the remaining retries."""
    sleeper = _RecordingSleep()
    c = FakeDriveClient(sleep=sleeper)
    c.upload("a")
    c.fail_next_with(403, "rateLimitExceeded", op="files.get")   # attempt 1
    c.fail_next_with(403, "forbidden", op="files.get")          # attempt 2
    with pytest.raises(PermissionDeniedError):
        c.get_metadata("a")
    assert c.retry_count() == 1          # retried once (for the rate limit), then stopped
    assert len(sleeper.delays) == 1


def test_retry_after_header_is_honored_verbatim():
    """A Retry-After of 12s is slept exactly, not jittered into something else."""
    sleeper = _RecordingSleep()
    c = FakeDriveClient(sleep=sleeper)
    c.upload("a")
    c.fail_next_with(429, "tooManyRequests", op="files.get", retry_after="12")
    c.get_metadata("a")
    assert c.retry_count() == 1
    assert sleeper.delays == [12.0]


def test_retry_after_only_applies_to_retryable_errors():
    """A denial carrying a Retry-After header must NOT be slept or retried."""
    sleeper = _RecordingSleep()
    c = FakeDriveClient(sleep=sleeper)
    c.upload("a")
    c.fail_next_with(403, "forbidden", op="files.get", retry_after="30")
    with pytest.raises(PermissionDeniedError):
        c.get_metadata("a")
    assert sleeper.delays == []
    assert c.retry_count() == 0


def test_backoff_is_exponential_with_decorrelated_jitter_and_capped():
    """Bounded shape: every delay in (0, cap], growth roughly exponential."""
    deterministic = iter([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
    sleeps: list[float] = []
    prev = None
    for attempt in range(8):
        prev = backoff(attempt, sleep=sleeps.append,
                       rng=lambda: next(deterministic), previous=prev)
    assert all(0 < d <= 60 for d in sleeps)
    # upper bound grows exponentially; the shape must not explode past it
    assert sleeps[0] <= 1 and sleeps[-1] <= 60


# --------------------------------------------------------------------- changes


def test_changes_list_returns_next_page_and_new_start_tokens():
    """A multi-page change stream: page 1 carries nextPageToken and NO
 newStartPageToken; the final page flips that around (the build plan cursor loop)."""
    c = make_client(changes_page_size=2)
    for i in range(3):
        c.upload(f"f{i}")
    start = c.get_start_page_token()

    for i in range(3):  # 3 renames -> 3 changes after the start token
        c.rename(f"f{i}", f"f{i}-renamed")

    page1 = c.changes_list(start)
    assert len(page1.changes) == 2
    assert page1.next_page_token is not None
    assert page1.new_start_page_token is None      # only the LAST page has it

    page2 = c.changes_list(page1.next_page_token)
    assert len(page2.changes) == 1
    assert page2.next_page_token is None
    assert page2.new_start_page_token is not None  # the new cursor to persist

    # Each page cost 100 units.
    assert c.quota.snapshot().call_counts["changes_list"] == 2
    assert c.quota.units_used >= 2 * COST_LIST_CHANGES


def test_changes_replaying_from_new_start_token_yields_nothing():
    """The invariant of the cursor loop: after committing at the new start
 token, a drain must see zero changes (idempotent replay)."""
    c = make_client()
    c.upload("a")
    start = c.get_start_page_token()
    c.rename("a", "renamed")
    page = c.changes_list(start)
    fresh = c.changes_list(page.new_start_page_token)
    assert fresh.changes == []
    assert fresh.new_start_page_token is not None


def test_changes_removed_entries_cover_deletion_and_loss_of_access():
    """removed=true tombstones regardless of fetchability (delete AND unshare)."""
    c = make_client()
    c.upload("a")
    c.upload("b")
    start = c.get_start_page_token()
    c.unshare("a")        # loss of access -> removed
    c.service.remove("b")  # hard delete -> removed

    page = c.changes_list(start)
    by_id = {e["fileId"]: e for e in page.changes}
    assert by_id["a"]["removed"] is True
    assert by_id["b"]["removed"] is True
    assert "file" not in by_id["a"]      # removed entries carry no metadata
    assert "file" not in by_id["b"]


def test_get_start_page_token_precedes_enumeration():
    """the build plan: the token is taken BEFORE enumeration, so the first
 ingest can replay changes that happen during it."""
    c = make_client()
    start_before = c.get_start_page_token()
    c.upload("a")  # change lands after the snapshot
    changes = c.changes_list(start_before).changes
    assert [e["fileId"] for e in changes] == ["a"]


def test_changes_metadata_only_change_keeps_md5_stable():
    """Rename bumps Drive's version but leaves md5Checksum alone - the
 metadata-vs-content split the sync layer keys on."""
    c = make_client()
    c.upload("a", name="old", content=b"bytes", md5_checksum="md5-v1")
    start = c.get_start_page_token()
    c.rename("a", "new")
    entry = c.changes_list(start).changes[0]
    assert entry["file"]["version"] == "2"
    assert entry["file"]["md5Checksum"] == "md5-v1"   # unchanged
    assert entry["file"]["name"] == "new"


def test_edit_content_change_updates_checksum_and_version():
    c = make_client()
    c.upload("a", content=b"v1", md5_checksum="md5-v1")
    start = c.get_start_page_token()
    c.edit("a", b"v2 longer", md5_checksum="md5-v2", version="7")
    entry = c.changes_list(start).changes[0]
    assert entry["file"]["md5Checksum"] == "md5-v2"
    assert entry["file"]["version"] == "7"
    assert entry["file"]["size"] == "9"


def test_move_and_trash_restore_round_trip():
    c = make_client()
    c.upload("a", parents=["root"])
    start = c.get_start_page_token()
    c.move("a", ["folder-x"])
    assert next(iter(c.list_files()))["parents"] == ["folder-x"]
    c.trash("a")
    assert next(iter(c.list_files()))["trashed"] is True
    c.restore("a")
    assert next(iter(c.list_files()))["trashed"] is False
    # three metadata-only changes, same md5 throughout
    entries = c.changes_list(start).changes
    assert len(entries) == 3
    assert all(e["file"]["md5Checksum"] == "" for e in entries)


# ------------------------------------------------------- resource-key downloads


def test_resource_key_header_attached_when_present():
    """Link-shared resource-key files need X-Goog-Drive-Resource-Keys on the
 media request, formatted '<fileId>/<resourceKey>' (the build plan)."""
    c = make_client()
    c.upload("shared1", content=b"secret-doc", resource_key="rk-123")
    body = c.download_media("shared1", resource_key="rk-123")
    assert body == b"secret-doc"

    media = [r for r in c.service.requests if r["op"] == "files.get_media"]
    assert len(media) == 1
    assert media[0]["headers"]["X-Goog-Drive-Resource-Keys"] == "shared1/rk-123"


def test_no_resource_key_header_when_absent():
    c = make_client()
    c.upload("plain", content=b"ok")
    c.download_media("plain")
    media = [r for r in c.service.requests if r["op"] == "files.get_media"]
    assert "X-Goog-Drive-Resource-Keys" not in media[0]["headers"]


# --------------------------------------------------------------- canDownload gate


def test_candownload_false_denies_before_any_media_request():
    """canDownload=False raises PermissionDeniedError BEFORE the 200-unit call:
 no files.get_media request may appear, and only the 5-unit gate read is spent."""
    c = make_client()
    c.upload("locked", content=b"nope", can_download=False)

    with pytest.raises(PermissionDeniedError):
        c.download_media("locked")

    ops = [r["op"] for r in c.service.requests]
    assert "files.get_media" not in ops            # the download never happened
    assert ops.count("files.get") == 1             # the gate's metadata read
    assert c.retry_count() == 0
    assert c.quota.snapshot().errors == {"permission": 1}
    # gate read is charged, the download is not
    assert c.quota.units_used == COST_METADATA_READ


def test_candownload_true_downloads_and_charges_full_price():
    c = make_client()
    c.upload("open", content=b"data", can_download=True)
    assert c.download_media("open") == b"data"
    assert c.quota.units_used == COST_METADATA_READ + COST_DOWNLOAD


def test_export_returns_bytes_and_charges_200():
    c = make_client()
    c.upload("gdoc", mime_type="application/vnd.google-apps.document", content=b"docx-bytes")
    out = c.export("gdoc", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert out == b"docx-bytes"
    assert c.quota.snapshot().call_counts["export"] == 1
    assert c.quota.units_used == COST_EXPORT


def test_download_of_missing_file_is_404_not_forgotten():
    """A missing file surfaces as the typed FenrisDriveError for its 404, not
 as an unclassified exception (sync's unreadable path depends on that)."""
    from fenris.drive.errors import FenrisDriveError

    c = make_client()
    with pytest.raises(FenrisDriveError) as exc_info:
        c.download_media("never-existed")
    assert exc_info.value.status_code == 404


# ------------------------------------------------------------------------ auth


class _FakeKeyringModule:
    """keyring.get_password/set_password/delete_password triple for tests."""

    def __init__(self, stored: str | None = None):
        self.stored = stored
        self.calls: list[tuple[str, str, str | None]] = []

    def get_password(self, service, account):
        self.calls.append(("get", service, account))
        return self.stored

    def set_password(self, service, account, value):
        self.calls.append(("set", service, account))
        self.stored = value

    def delete_password(self, service, account):
        self.calls.append(("delete", service, account))
        self.stored = None


def test_disconnect_revokes_and_deletes_keyring_entry(monkeypatch):
    """THE security rule: disconnect POSTs to the revoke endpoint with the
 token AND removes the keyring entry - no stale token lingers silently."""
    opener = FakeOpener(status=200)
    ring = FakeKeyring(stored="refresh-token-1")

    revoked = auth_mod.disconnect(_opener=lambda: opener,
                                  _keyring_get=ring.get_password,
                                  _keyring_delete=ring.delete_password)

    assert revoked is True
    assert len(opener.requests) == 1
    request = opener.requests[0]
    assert request.full_url == REVOKE_URL
    assert request.get_method() == "POST"
    body = request.data.decode("utf-8")
    assert "token=refresh-token-1" in body
    assert ring.deleted == [(KEYRING_SERVICE, KEYRING_ACCOUNT)]
    assert ring.stored is None


def test_disconnect_deletes_keyring_even_when_revoke_fails(monkeypatch):
    """Revoke rejected: the local entry still goes - never claim connected."""
    opener = FakeOpener(status=500)
    ring = FakeKeyring(stored="refresh-token-1")

    with pytest.raises(AuthError):
        auth_mod.disconnect(_opener=lambda: opener,
                            _keyring_get=ring.get_password,
                            _keyring_delete=ring.delete_password)

    assert ring.stored is None
    assert ring.deleted == [(KEYRING_SERVICE, KEYRING_ACCOUNT)]


def test_disconnect_with_no_stored_token_is_a_noop(monkeypatch):
    opener = FakeOpener(status=200)
    ring = FakeKeyring(stored=None)
    assert auth_mod.disconnect(_opener=lambda: opener,
                               _keyring_get=ring.get_password,
                               _keyring_delete=ring.delete_password) is False
    assert opener.requests == []          # nothing to revoke


def test_auth_flow_uses_pkce_and_readonly_scope(tmp_path, monkeypatch):
    """The flow is InstalledAppFlow with drive.readonly and PKCE on."""
    secrets = tmp_path / "client_secrets.json"
    secrets.write_text(json.dumps({"installed": {
        "client_id": "cid", "client_secret": "cs",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost:8080"],
    }}))

    captured: dict = {}

    class FakeCreds:
        refresh_token = DUMMY_TOKEN_FOR_TESTS

    class FakeFlow:
        def __init__(self, scopes, autogenerate_code_verifier=None):
            captured["scopes"] = scopes
            captured["autogenerate_code_verifier"] = autogenerate_code_verifier
            self.code_verifier = "v" * 64

        @classmethod
        def from_client_config(cls, config, scopes, **kwargs):
            return cls(scopes, **kwargs)

        def run_local_server(self, host="localhost", port=8080, open_browser=True,
                             timeout_seconds=None):
            captured["host"] = host
            captured["port"] = port
            return FakeCreds()

    import fenris.drive.auth as A
    monkeypatch.setattr(A, "InstalledAppFlow", FakeFlow, raising=False)
    ring = _FakeKeyringModule()
    monkeypatch.setattr(A.keyring, "set_password", ring.set_password)
    monkeypatch.setattr(A.keyring, "get_password", ring.get_password)

    class _UsableBackend:  # not null.Keyring / fail.Keyring -> passes the gate
        pass

    monkeypatch.setattr(A.keyring, "get_keyring", lambda: _UsableBackend())

    token = A.run_auth_flow(secrets)
    assert token == DUMMY_TOKEN_FOR_TESTS
    assert captured["scopes"] == [A.DRIVE_SCOPE]
    assert A.DRIVE_SCOPE == "https://www.googleapis.com/auth/drive.readonly"
    assert captured["autogenerate_code_verifier"] is True   # PKCE loopback
    assert ring.stored == DUMMY_TOKEN_FOR_TESTS
    assert ("set", KEYRING_SERVICE, KEYRING_ACCOUNT) in ring.calls


def test_run_auth_flow_requires_installed_type_secrets(tmp_path):
    import fenris.drive.auth as A

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"web": {"client_id": "x"}}))
    with pytest.raises(AuthError):
        A.run_auth_flow(bad)
    missing = tmp_path / "missing.json"
    with pytest.raises(AuthError):
        A.run_auth_flow(missing)


def test_keyring_unavailable_raises_with_instructions_never_plaintext(monkeypatch, tmp_path):
    """No keyring backend -> clear instructions, NOT a plaintext fallback.

 run_auth_flow must fail before any OAuth work happens, and it must never
 write the token to disk: after the failure, no file exists under the
 caller's tree.
 """
    from keyring.backends import null as null_backend

    monkeypatch.setattr(auth_mod.keyring, "get_keyring", lambda: null_backend.Keyring())
    # Give the flow a VALID secrets file so the keyring check is what fails.
    secrets = tmp_path / "client_secrets.json"
    secrets.write_text(json.dumps({"installed": {
        "client_id": "cid", "client_secret": "cs",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost:8080"],
    }}))
    with pytest.raises(KeyringUnavailableError) as exc:
        auth_mod.run_auth_flow(secrets)
    message = str(exc.value)
    assert "keyring" in message.lower()
    assert "plaintext file" in message     # we name the thing we refuse to do
    # The only file in the tree is the secrets we wrote; no token file appeared.
    assert [p.name for p in tmp_path.iterdir()] == ["client_secrets.json"]


def test_load_or_refresh_without_token_tells_user_to_auth(monkeypatch):
    """No stored token -> a clear AuthError pointing at `fenris drive auth`,
 never a silent empty credential that sync would treat as connected."""
    ring = _FakeKeyringModule(stored=None)
    monkeypatch.setattr(auth_mod.keyring, "get_password", ring.get_password)
    from keyring.backends import SecretService  # a REAL backend class so
    # _require_keyring passes; only the stored value is empty.
    monkeypatch.setattr(auth_mod.keyring, "get_keyring",
                        lambda: SecretService.Keyring.__new__(SecretService.Keyring))
    with pytest.raises(AuthError) as exc:
        auth_mod.load_or_refresh()
    assert "drive auth" in str(exc.value)


# ------------------------------------------------- fake/real contract parity


def test_fake_is_drop_in_for_the_real_client_contract():
    """FakeDriveClient exposes exactly the DriveClient surface the sync layer
 calls - same names, same signatures - so the freshness suite and the real
 client cannot drift apart."""
    import inspect

    real = DriveClient.__dict__
    fake = FakeDriveClient.__dict__
    for name in (
        "list_files", "get_metadata", "download_media", "export",
        "get_start_page_token", "changes_list",
    ):
        assert name in fake, f"FakeDriveClient is missing {name}"
        assert name in real, f"DriveClient is missing {name}"
        real_sig = inspect.signature(real[name])
        fake_sig = inspect.signature(fake[name])
        assert list(fake_sig.parameters) == list(real_sig.parameters), (
            f"{name} parameter drift between real and fake: "
            f"{list(fake_sig.parameters)} vs {list(real_sig.parameters)}"
        )


def test_fake_counts_quota_on_the_same_cost_table_as_the_real_client():
    """The fake charges what production pays for an identical op mix - this is
 what makes 'quota units/run' numbers from offline eval meaningful."""
    from fenris.drive.fake import FakeDriveService

    def seed(svc):
        svc.upload("a", content=b"x", md5_checksum="m1",
                   mime_type="application/vnd.google-apps.document")

    real_service = FakeDriveService()
    fake_client = FakeDriveClient()
    seed(real_service)
    seed(fake_client.service)

    real_client = DriveClient(real_service)                  # real client, fake wire
    for client in (real_client, fake_client):
        client.get_metadata("a")
        client.download_media("a")
        client.export("a", "application/pdf")
        client.get_start_page_token()
        client.changes_list(client.get_start_page_token())

    assert fake_client.quota.snapshot() == real_client.quota.snapshot()
    # 2x metadata (5) + download (200) + export (200) + 2x start-token (100)
    # + changes (100) = 710 on both.
    assert real_client.quota.units_used == 710
    assert fake_client.quota.units_used == 710


# ------------------------------------------------------------------------- CLI


def _wire_cli():
    """drive_app as a sub-command of a root app, as the integrator will wire it."""
    import typer

    from fenris.drive.cli import drive_app

    root = typer.Typer()
    root.add_typer(drive_app, name="drive")
    return root, CliRunner()


def test_cli_status_reflects_missing_token():
    root, runner = _wire_cli()
    result = runner.invoke(root, ["drive", "status"])
    assert result.exit_code == 0
    assert "not found" in result.output
    assert "0 units over 0 calls" in result.output


def test_cli_status_reports_keyring_token(monkeypatch):
    root, runner = _wire_cli()
    ring = _FakeKeyringModule(stored="tok")
    monkeypatch.setattr(auth_mod.keyring, "get_password", ring.get_password)
    from keyring.backends import SecretService

    monkeypatch.setattr(auth_mod.keyring, "get_keyring",
                        lambda: SecretService.Keyring.__new__(SecretService.Keyring))
    result = runner.invoke(root, ["drive", "status"])
    assert result.exit_code == 0
    assert "present" in result.output
    assert auth_mod.DRIVE_SCOPE in result.output


def test_cli_disconnect_without_token_is_graceful(monkeypatch):
    """`disconnect` with nothing stored must not crash - it says so and exits 0."""
    root, runner = _wire_cli()
    monkeypatch.setattr(auth_mod, "disconnect", lambda **_kw: False)
    result = runner.invoke(root, ["drive", "disconnect"])
    assert result.exit_code == 0
    assert "Nothing to disconnect" in result.output


def test_cli_files_list_requires_auth_first(monkeypatch):
    """With no token stored, `files list` fails fast with the auth instruction."""
    root, runner = _wire_cli()
    ring = _FakeKeyringModule(stored=None)
    monkeypatch.setattr(auth_mod.keyring, "get_password", ring.get_password)
    from keyring.backends import SecretService

    monkeypatch.setattr(auth_mod.keyring, "get_keyring",
                        lambda: SecretService.Keyring.__new__(SecretService.Keyring))
    result = runner.invoke(root, ["drive", "files", "list", "--limit", "5"])
    assert result.exit_code == 2
    assert "drive auth" in result.output


def test_cli_files_list_limit_stops_enumeration(monkeypatch):
    """--limit N stops the generator after N items: a bounded-cost smoke run."""
    root, runner = _wire_cli()

    class _FakeService:
        def __init__(self):
            from fenris.drive.fake import FakeDriveService

            self._inner = FakeDriveService()
            self.quota_hook = None

        def files(self):
            return self._inner.files()

        def changes(self):
            return self._inner.changes()

    svc = _FakeService()
    for i in range(30):
        svc._inner.upload(f"f{i}", name=f"file {i}")

    class _FakeCreds:
        pass

    monkeypatch.setattr(auth_mod, "load_or_refresh", lambda: _FakeCreds())

    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: svc)

    result = runner.invoke(root, ["drive", "files", "list", "--limit", "3"])
    assert result.exit_code == 0
    lines = [ln for ln in result.output.splitlines() if "\t" in ln]
    assert len(lines) == 3
    assert lines[0].startswith("f0")
    assert "quota this session" in result.output
    # only ONE page fetched for 3 of 30 files
    assert [r["op"] for r in svc._inner.requests].count("files.list") == 1
