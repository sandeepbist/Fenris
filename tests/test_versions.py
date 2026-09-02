"""Versioned-document QA tests: revisions list, chunk diff, honest misses."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fenris.config import FenrisConfig
from fenris.db import connect
from fenris.ingest.pipeline import FileMeta, ingest_file


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    """One file with two revisions: v1 -> v2 (edited + a clause dropped)."""
    monkeypatch.setenv("FENRIS_HOME", str(tmp_path / "home"))
    cfg = FenrisConfig(data_dir=tmp_path / "home")
    conn = connect(cfg.db_path)

    ingest_file(conn, None, FileMeta(
        drive_id="mydrive", file_id="contract", title="Contract terms",
        mime_type="text/plain", source_revision="rev1"),
        b"Clause 1: payment terms are net 30. "
        b"Clause 2: termination requires 60 days notice. "
        b"Clause 3: governing law is the home state.")

    ingest_file(conn, None, FileMeta(
        drive_id="mydrive", file_id="contract", title="Contract terms",
        mime_type="text/plain", source_revision="rev2"),
        b"Clause 1: payment terms are net 45. "
        b"Clause 3: governing law is the home state.")
    conn.commit()

    rows = conn.execute(
        "SELECT id FROM sources WHERE file_id = ? ORDER BY id",
        ("contract",),
    ).fetchall()
    old_id, new_id = rows[0][0], rows[1][0]
    conn.close()

    from fenris.api import app

    return TestClient(app), old_id, new_id


def test_revisions_listed_newest_first(seeded):
    tc, old_id, new_id = seeded
    r = tc.get("/api/files/mydrive/contract/revisions")
    assert r.status_code == 200
    revs = r.json()["revisions"]
    assert len(revs) == 2
    assert revs[0]["source_id"] == new_id and revs[0]["active"] is True
    assert revs[1]["source_id"] == old_id and revs[1]["active"] is False


def test_diff_reports_added_and_removed(seeded):
    tc, old_id, new_id = seeded
    r = tc.get(f"/api/files/mydrive/contract/diff?from_id={old_id}&to_id={new_id}")
    assert r.status_code == 200
    body = r.json()
    added_text = " ".join(a["body"] for a in body["added"])
    removed_text = " ".join(a["body"] for a in body["removed"])
    assert "net 45" in added_text
    assert "net 30" in removed_text
    assert "60 days" in removed_text
    assert "governing law" in added_text or body["unchanged_count"] >= 1
    assert "chunk(s) added" in body["summary"]


def test_diff_identical_revisions_says_so(seeded):
    tc, _old_id, new_id = seeded
    r = tc.get(f"/api/files/mydrive/contract/diff?from_id={new_id}&to_id={new_id}")
    assert r.status_code == 200
    assert "identical content" in r.json()["summary"]
    assert r.json()["added"] == [] and r.json()["removed"] == []


def test_diff_hostile_ids_404(seeded):
    tc, _, _ = seeded
    r = tc.get("/api/files/mydrive/contract/diff?from_id=9999&to_id=1")
    assert r.status_code == 404
    # ids from a DIFFERENT file must not leak through
    r2 = tc.get("/api/files/mydrive/other-file/diff?from_id=1&to_id=2")
    assert r2.status_code == 404


def test_revisions_unknown_file_empty(seeded):
    tc, _, _ = seeded
    r = tc.get("/api/files/mydrive/no-such-file/revisions")
    assert r.status_code == 200
    assert r.json()["revisions"] == []
