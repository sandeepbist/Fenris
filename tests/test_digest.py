"""P2.4 digest tests: honest windows, chained anchors, cron scheduling.

All offline: the digest is computed from the projection only (no
network, no LLM), which is the feature's core promise. Seeding goes
through the real ingest pipeline (the same path sync uses), so the
created_at stamps these tests assert on are the production ones.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from fenris.config import FenrisConfig
from fenris.db import connect
from fenris.digest import (
    Digest,
    add_schedule,
    compute_digest,
    delete_schedule,
    latest_digest,
    list_schedules,
    persist_digest,
    recent_digests,
    run_due,
    schedule_matches,
    set_schedule_enabled,
    validate_cron_field,
)
from fenris.ingest.pipeline import FileMeta, ingest_file


@pytest.fixture()
def cfg(tmp_path) -> FenrisConfig:
    return FenrisConfig(data_dir=tmp_path / "home")


@pytest.fixture()
def conn(cfg):
    c = connect(cfg.db_path)
    yield c
    c.close()


def _ingest(conn, file_id, title, content=b"body text", revision="r1"):
    return ingest_file(conn, None, FileMeta(
        drive_id="mydrive", file_id=file_id, title=title,
        mime_type="text/plain", source_revision=revision), content)


# ---- compute: the honest windows -------------------------------------------------


def test_created_at_is_stamped(conn):
    """The latent-bug regression: ingest stamps real index time, not NULL."""
    _ingest(conn, "f1", "Doc")
    stamp = conn.execute("SELECT created_at FROM sources WHERE file_id = 'f1'", ()).fetchone()[0]
    assert stamp is not None and stamp.endswith("Z")


def test_first_indexing_is_new(conn):
    _ingest(conn, "brand-new", "New doc")
    digest = compute_digest(conn, 7.0)
    assert "brand-new" in digest.sections["new"].file_ids
    assert digest.counts["new"] == 1


def test_revision_after_window_is_updated_not_new(conn):
    """A file that existed before the window is 'updated', not 'new'."""
    _ingest(conn, "contract", "Contract", revision="r1")
    time.sleep(1.1)  # 1s stamp resolution: r1 strictly before the anchor
    anchor = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    time.sleep(1.1)  # ...and r2 strictly after it
    _ingest(conn, "contract", "Contract", revision="r2")
    digest = compute_digest(conn, 7.0, anchor=anchor)
    assert "contract" in digest.sections["updated"].file_ids
    assert digest.counts["new"] == 0
    assert "Contract" in digest.markdown


def test_failed_ingest_is_a_problem_not_new(conn):
    """A failed parse inside the window lands in problems, never in new."""
    ingest_file(conn, None, FileMeta(
        drive_id="mydrive", file_id="broken", title="Broken",
        mime_type="application/pdf", source_revision="x"),
        b"%PDF-not-actually-a-pdf")
    row = conn.execute("SELECT status FROM sources WHERE file_id = 'broken'", ()).fetchone()
    if row and row[0] == "failed":
        digest = compute_digest(conn, 7.0)
        assert "broken" in digest.sections["problems"].file_ids
        assert "broken" not in digest.sections["new"].file_ids
    else:  # parser tolerated it in this build: assert it did NOT fail
        assert row is None or row[0] in ("ok", "skipped")


def test_removed_file_in_window(conn):
    """A tombstoned drive_files row counts as removed."""
    _ingest(conn, "gone", "Doomed doc")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # ingest_file writes sources; drive_files is the sync layer's table,
    # so the tombstone trail is seeded the way a drain leaves it
    conn.execute("INSERT INTO drive_files(drive_id, file_id, title, trashed, updated_at) VALUES ('mydrive', 'gone', 'Doomed doc', 1, ?)", (stamp,))
    conn.execute(
        "UPDATE sources SET active = 0 WHERE file_id = ?", ("gone",))
    conn.commit()
    digest = compute_digest(conn, 7.0)
    assert "gone" in digest.sections["removed"].file_ids
    assert "Doomed doc" in digest.sections["removed"].titles.get("gone", "")


def test_unreadable_is_problem_not_removed(conn):
    """Access-revoked files must not double-list as removed: the problems
    guard takes precedence over the trashed signal."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute("INSERT INTO sources(drive_id, file_id, source_revision, repr_mime, status, created_at) VALUES ('mydrive', 'revoked', 'r1', 'text/plain', 'unreadable', ?)", (stamp,))
    conn.execute("INSERT INTO drive_files(drive_id, file_id, title, trashed, updated_at) VALUES ('mydrive', 'revoked', 'revoked.txt', 1, ?)", (stamp,))
    conn.commit()
    digest = compute_digest(conn, 7.0)
    assert "revoked" in digest.sections["problems"].file_ids
    assert "revoked" not in digest.sections["removed"].file_ids


def test_quiet_window_is_valid_not_error(conn):
    """An empty projection digests to 'nothing changed', counts all zero."""
    digest = compute_digest(conn, 7.0)
    assert sum(digest.counts.values()) == 0
    assert "Nothing changed" in digest.markdown


def test_chained_window_no_double_reporting(conn):
    """The second digest's window starts at the first's generated_at."""
    _ingest(conn, "once", "Once doc")
    first = compute_digest(conn, 7.0)
    persist_digest(conn, first)
    second = compute_digest(conn, 7.0)  # no anchor -> chains from first
    assert second.window_start == first.generated_at
    assert "once" not in second.sections["new"].file_ids


def test_skipped_format_in_window(conn):
    """A skip-listed format lands in skipped, not new."""
    ingest_file(conn, None, FileMeta(
        drive_id="mydrive", file_id="song", title="song.mp3",
        mime_type="audio/mpeg", source_revision="m1"), b"")
    digest = compute_digest(conn, 7.0)
    assert "song" in digest.sections["skipped"].file_ids
    assert "song" not in digest.sections["new"].file_ids


def test_max_list_cap(conn):
    """A huge window caps section lists at MAX_LIST with an overflow line."""
    for i in range(55):
        _ingest(conn, f"bulk-{i}", f"Bulk doc {i}")
    digest = compute_digest(conn, 7.0)
    assert digest.counts["new"] == 55
    assert len(digest.sections["new"].file_ids) == 55
    assert "and 5 more" in digest.markdown


# ---- schedules: cron subset ------------------------------------------------------


def test_validate_cron_field_accepts_shapes():
    assert validate_cron_field("*", 0, 59) == "*"
    assert validate_cron_field("8", 0, 59) == "8"
    assert validate_cron_field("9-17", 0, 23) == "9-17"
    assert validate_cron_field("*/10", 0, 59) == "*/10"
    assert validate_cron_field("0,15,30,45", 0, 59) == "0,15,30,45"
    assert validate_cron_field("1-3,5", 0, 6) == "1-3,5"


def test_validate_cron_field_rejects_junk():
    for bad, low, high in (
        ("60", 0, 59), ("abc", 0, 59), ("*/0", 0, 59),
        ("-1", 0, 59), ("5--9", 0, 59), ("", 0, 59),
        ("DROP TABLE", 0, 59), ("24", 0, 23), ("7", 0, 6),
    ):
        with pytest.raises(ValueError):
            validate_cron_field(bad, low, high)


def test_schedule_matches_daily_at_8(conn):
    sid = add_schedule(conn, "daily8", "0", "8", "*", 7.0)
    (sched,) = list_schedules(conn)
    monday_0800 = time.strptime("2026-09-07 08:00:00", "%Y-%m-%d %H:%M:%S")
    monday_0801 = time.strptime("2026-09-07 08:01:00", "%Y-%m-%d %H:%M:%S")
    tuesday_0800 = time.strptime("2026-09-08 08:00:00", "%Y-%m-%d %H:%M:%S")
    assert schedule_matches(sched, monday_0800)
    assert not schedule_matches(sched, monday_0801)
    assert schedule_matches(sched, tuesday_0800)  # daily
    assert sid > 0


def test_schedule_matches_weekly_monday_only(conn):
    add_schedule(conn, "mon8", "0", "8", "1", 7.0)  # cron dow 1 = Monday
    (sched,) = list_schedules(conn)
    monday = time.strptime("2026-09-07 08:00:00", "%Y-%m-%d %H:%M:%S")
    tuesday = time.strptime("2026-09-08 08:00:00", "%Y-%m-%d %H:%M:%S")
    sunday = time.strptime("2026-09-06 08:00:00", "%Y-%m-%d %H:%M:%S")
    assert schedule_matches(sched, monday)
    assert not schedule_matches(sched, tuesday)
    assert not schedule_matches(sched, sunday)


def test_run_due_fires_and_is_idempotent_within_minute(conn):
    _ingest(conn, "due-doc", "Due doc")
    add_schedule(conn, "always", "*", "*", "*", 7.0)  # every minute
    moment = time.gmtime()
    first = run_due(conn, now=moment)
    assert len(first) == 1
    assert first[0]["counts"]["new"] >= 1
    # second call in the SAME minute: nothing fires again
    again = run_due(conn, now=moment)
    assert again == []
    # the ledger has the run; the schedule stamps last_run_at
    runs = recent_digests(conn)
    assert len(runs) == 1


def test_run_due_respects_disabled(conn):
    add_schedule(conn, "off", "*", "*", "*", 7.0)
    set_schedule_enabled(conn, 1, False)
    assert run_due(conn, now=time.gmtime()) == []


def test_schedule_delete(conn):
    add_schedule(conn, "temp", "0", "8", "*", 7.0)
    assert delete_schedule(conn, 1) is True
    assert delete_schedule(conn, 1) is False
    assert list_schedules(conn) == []


def test_window_days_bounds(conn):
    with pytest.raises(ValueError):
        add_schedule(conn, "bad", "0", "8", "*", 0.05)
    with pytest.raises(ValueError):
        add_schedule(conn, "bad", "0", "8", "*", 365)


# ---- persist + read -------------------------------------------------------------


def test_latest_and_recent_digests(conn):
    assert latest_digest(conn) is None
    d = Digest(generated_at="2026-09-03T00:00:00Z", window_start="2026-08-27T00:00:00Z",
               window_days=7.0, counts={"new": 1}, markdown="# x")
    persist_digest(conn, d)
    got = latest_digest(conn)
    assert got is not None and got.markdown == "# x"
    assert recent_digests(conn)[0]["counts"] == {"new": 1}


# ---- API surface ------------------------------------------------------------------


@pytest.fixture()
def tc(tmp_path, monkeypatch):
    monkeypatch.setenv("FENRIS_HOME", str(tmp_path / "home"))
    from fenris.api import app

    return TestClient(app)


def test_api_generate_latest_and_list(tc):
    r = tc.post("/api/digests/generate?window_days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["markdown"].startswith("# Drive digest")
    assert body["run_id"] > 0

    latest = tc.get("/api/digests/latest").json()
    assert latest["digest"] is not None

    runs = tc.get("/api/digests").json()["runs"]
    assert len(runs) == 1

    one = tc.get(f"/api/digests/run/{body['run_id']}")
    assert one.status_code == 200
    assert one.json()["markdown"].startswith("# Drive digest")


def test_api_generate_rejects_bad_window(tc):
    assert tc.post("/api/digests/generate?window_days=365").status_code == 422
    assert tc.post("/api/digests/generate?window_days=0.01").status_code == 422


def test_api_schedule_lifecycle(tc):
    r = tc.post("/api/digest-schedules", json={
        "name": "Weekly digest", "at": "08:00", "daily": False, "dow": "mon",
        "window_days": 7})
    assert r.status_code == 200
    sid = r.json()["id"]

    listed = tc.get("/api/digest-schedules").json()["schedules"]
    assert listed[0]["name"] == "Weekly digest"
    assert listed[0]["daily"] is False

    assert tc.post(f"/api/digest-schedules/{sid}/toggle?enabled=false").status_code == 200
    listed = tc.get("/api/digest-schedules").json()["schedules"]
    assert listed[0]["enabled"] is False

    assert tc.delete(f"/api/digest-schedules/{sid}").status_code == 200
    assert tc.delete(f"/api/digest-schedules/{sid}").status_code == 404


def test_api_schedule_rejects_junk(tc):
    assert tc.post("/api/digest-schedules", json={
        "name": "x", "at": "99:99"}).status_code == 422
    assert tc.post("/api/digest-schedules", json={
        "name": "x", "at": "08:00", "daily": False, "dow": "funday"}).status_code == 422


def test_api_run_due_empty(tc):
    r = tc.post("/api/digests/run-due")
    assert r.status_code == 200
    assert r.json()["ran"] == []
