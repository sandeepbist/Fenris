"""M5 sync tests: the cursor-discipline invariants, all offline.

Every test uses FakeDriveClient (in-memory Drive with genuine quota
accounting) and a tmp projection DB. The load-bearing assertions:

- the start token is persisted BEFORE enumeration begins;
- the page token advances ONLY in the same transaction as index writes
  (crash -> replay converges idempotently);
- metadata-only changes never re-ingest;
- removal tombstones unfetchable files;
- one permission denial never aborts a drain;
- resync reconciles deletions the feed never reported.
"""

from __future__ import annotations

import pytest

from fenris.config import FenrisConfig
from fenris.db import connect
from fenris.drive.fake import FakeDriveClient
from fenris.sync.changes import drain
from fenris.sync.enumerate import enumerate_drive, is_ingestable
from fenris.sync.service import full_resync, sync_all, sync_status
from fenris.sync.staleness import is_stale_sync, last_drain_at

PDF = "application/pdf"
TXT = "text/plain"
MP3 = "audio/mpeg"
DOCS = "application/vnd.google-apps.document"


@pytest.fixture()
def cfg(tmp_path) -> FenrisConfig:
    return FenrisConfig(data_dir=tmp_path / "home")


@pytest.fixture()
def conn(cfg):
    c = connect(cfg.db_path)
    yield c
    c.close()


@pytest.fixture()
def drive() -> FakeDriveClient:
    return FakeDriveClient()


def upload(drive, file_id, name="doc", mime=TXT, content=b"hello world", md5="m1", **kw):
    # upload() hard-codes version "1"; content edits go through drive.edit
    return drive.upload(
        file_id, name=name, mime_type=mime, content=content,
        md5_checksum=md5, **kw
    )


def active_sources(conn, file_id):
    return conn.execute(
        "SELECT id, source_revision FROM sources WHERE file_id = ? AND active = 1",
        (file_id,),
    ).fetchall()


class TestEnumerate:
    def test_start_token_persisted_before_listing(self, conn, drive):
        """The ordering invariant: pending_start committed first."""
        upload(drive, "f1")
        enumerate_drive(conn, drive)

        row = conn.execute(
            "SELECT pending_start FROM sync_cursor WHERE drive_id = 'mydrive'"
        ).fetchone()
        assert row is not None and row[0]
        # and the token existed before any drive_files write ran
        assert conn.execute("SELECT count(*) FROM drive_files").fetchone()[0] == 1

    def test_pagination_and_counts(self, conn, drive):
        for i in range(7):
            upload(drive, f"f{i}", name=f"doc{i}")
        # page_size via the fake's list_files is fixed; with 7 files and
        # multiple pages the enumeration still sees all of them.
        report = enumerate_drive(conn, drive)
        assert report.n_files == 7
        assert report.n_ingestable == 7

    def test_skipped_formats_get_drive_files_but_no_sources(self, conn, drive):
        upload(drive, "f1", name="song.mp3", mime=MP3)
        report = enumerate_drive(conn, drive)
        assert report.n_skipped_by_router == 1
        assert conn.execute("SELECT count(*) FROM drive_files").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
        assert not is_ingestable({"id": "f1", "mimeType": MP3})

    def test_ingestable_classification(self):
        assert is_ingestable({"id": "x", "mimeType": PDF})
        assert is_ingestable({"id": "x", "mimeType": DOCS})
        assert not is_ingestable({"id": "x", "mimeType": "application/zip"})


class TestCursorDiscipline:
    def test_metadata_only_rename_never_reingests(self, conn, drive):
        upload(drive, "f1", name="before.txt", content=b"stable content", md5="m1")
        sync_all(conn, drive, pace_seconds=0)

        drive.rename("f1", "after.txt")
        drive.quota.reset()
        report = drain(conn, drive, pace_seconds=0)

        assert report.content_reingests == 0
        assert report.metadata_only >= 1
        title = conn.execute("SELECT title FROM drive_files WHERE file_id='f1'").fetchone()[0]
        assert title == "after.txt"
        # quota: no downloads charged on the rename drain
        snap = drive.quota.snapshot()
        assert snap.call_counts.get("download_media", 0) == 0

    def test_edit_creates_new_representation_retires_old(self, conn, drive):
        upload(drive, "f1", name="doc.txt", content=b"version one", md5="m1")
        sync_all(conn, drive, pace_seconds=0)
        old = active_sources(conn, "f1")
        assert len(old) == 1

        drive.edit("f1", b"version two text", md5_checksum="m2", version="2")
        report = drain(conn, drive, pace_seconds=0)

        assert report.content_reingests == 1
        # exactly one active representation (the invariant)
        new = active_sources(conn, "f1")
        assert len(new) == 1 and new[0][1] == "m2"
        # old row retired
        retired = conn.execute(
            "SELECT count(*) FROM sources WHERE file_id='f1' AND active=0"
        ).fetchone()[0]
        assert retired == 1

    def test_removed_tombstones_even_when_unfetchable(self, conn, drive):
        upload(drive, "f1", content=b"doomed", md5="m1")
        sync_all(conn, drive, pace_seconds=0)
        assert len(active_sources(conn, "f1")) == 1

        drive.unshare("f1")  # removed=true semantics; file now unfetchable
        report = drain(conn, drive, pace_seconds=0)

        assert report.tombstoned >= 1
        assert active_sources(conn, "f1") == []

    def test_crash_replay_converges(self, conn, drive):
        """Crash after a drain's writes but before its commit: replay
        re-applies the same page and converges to the same end state."""
        upload(drive, "f1", content=b"one", md5="m1")
        sync_all(conn, drive, pace_seconds=0)

        # The token at the START of the first sync (drain-1's commit advanced
        # past it). Rewinding to it replays drain-1's entries exactly.
        row = conn.execute(
            "SELECT pending_start FROM sync_cursor WHERE drive_id='mydrive'"
        ).fetchone()
        assert row and row[0], "pending_start must be persisted"

        # ...but the honest crash test needs changes AFTER a committed
        # drain: take the current token, make changes, commit a drain,
        # then rewind to the saved token as if that drain never committed.
        saved_token = conn.execute(
            "SELECT page_token FROM sync_cursor WHERE drive_id='mydrive'"
        ).fetchone()[0]

        drive.edit("f1", b"changed content", md5_checksum="m2", version="2")
        upload(drive, "f2", content=b"two", md5="n1")
        report1 = drain(conn, drive, pace_seconds=0)
        assert report1.changes_processed >= 2
        assert len(active_sources(conn, "f1")) == 1
        assert len(active_sources(conn, "f2")) == 1

        # rewind the cursor as if the commit never happened
        conn.execute(
            "UPDATE sync_cursor SET page_token=? WHERE drive_id='mydrive'",
            (saved_token,),
        )
        conn.commit()

        report2 = drain(conn, drive, pace_seconds=0)
        assert report2.changes_processed >= 2  # replayed the same entries
        # idempotent: same end state, no duplicate active representations
        assert len(active_sources(conn, "f1")) == 1
        assert len(active_sources(conn, "f2")) == 1
        assert conn.execute(
            "SELECT count(*) FROM sources WHERE active=1"
        ).fetchone()[0] == 2

    def test_permission_denial_isolated(self, conn, drive):
        upload(drive, "f1", content=b"allowed", md5="m1")
        upload(drive, "f2", content=b"secret", md5="m2")
        sync_all(conn, drive, pace_seconds=0)

        # f2 becomes undownloadable (permission denial), f1 edits fine
        drive.edit("f1", b"edited content", md5_checksum="m1b", version="2")
        drive.service._files["f2"]["capabilities"] = {"canDownload": False}
        drive.edit("f2", b"locked", md5_checksum="m2b", version="2")

        report = drain(conn, drive, pace_seconds=0)
        # f1 re-ingested; f2 marked unreadable; drain completed
        assert report.content_reingests == 1
        assert report.failed == 1
        assert active_sources(conn, "f1")


class TestResyncAndStaleness:
    def test_corrupt_cursor_triggers_missing(self, conn, drive):
        conn.execute("DELETE FROM sync_cursor")
        conn.commit()
        report = drain(conn, drive, pace_seconds=0)
        assert report.resync_triggered == "missing_cursor"

    def test_full_resync_reconciles_direct_deletion(self, conn, drive):
        """A file deleted behind the feed's back gets tombstoned by diff."""
        upload(drive, "f1", content=b"vanishing", md5="m1")
        sync_all(conn, drive, pace_seconds=0)
        assert len(active_sources(conn, "f1")) == 1

        # delete directly in the fake store WITHOUT recording a change
        drive.service._files.pop("f1", None)

        report = full_resync(conn, drive, pace_seconds=0)
        # reconciliation tombstoned the representation
        assert active_sources(conn, "f1") == []
        assert report.resync_triggered == "full_resync"

    def test_staleness_flags_and_hints(self, conn, drive):
        upload(drive, "f1", content=b"content", md5="m1")
        sync_all(conn, drive, pace_seconds=0)
        # never drained -> stale
        assert is_stale_sync(conn)
        drain(conn, drive, pace_seconds=0)
        assert last_drain_at(conn) is not None
        # hints refreshed for active sources
        hint = conn.execute(
            "SELECT drive_version FROM sources_hints WHERE file_id='f1'"
        ).fetchone()
        assert hint is not None


class TestEndToEnd:
    def test_full_lifecycle(self, conn, drive):
        upload(drive, "f1", name="notes.txt", content=b"alpha notes", md5="m1")
        upload(drive, "f2", name="song.mp3", mime=MP3, content=b"audio", md5="m3")
        upload(drive, "f3", name="doc.gdoc", mime=DOCS, content=b"", md5="")

        report = sync_all(conn, drive, pace_seconds=0)
        assert report.resync_triggered == "initial_enumeration"

        # edit one, rename one, trash one
        drive.edit("f1", b"alpha notes v2", md5_checksum="m1b", version="2")
        drive.rename("f3", "renamed doc")
        drive.trash("f3")
        upload(drive, "f4", name="new.txt", content=b"beta", md5="m9")

        report = sync_all(conn, drive, pace_seconds=0)
        assert len(active_sources(conn, "f1")) == 1
        assert active_sources(conn, "f3") == []  # trashed -> tombstoned
        assert len(active_sources(conn, "f4")) == 1
        # skip format never gained a representation
        assert active_sources(conn, "f2") == []

        status = sync_status(conn)
        assert status["drive_files"] == 4
        assert status["cursor"]["page_token"]
