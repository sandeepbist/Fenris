"""Jump-to-source tests: the citation viewer endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fenris.config import FenrisConfig
from fenris.db import connect
from fenris.ingest.pipeline import FileMeta, ingest_file

BODY = (
    b"Opening paragraph of the quarterly report introducing the scope. "
    b"The revenue section states that Q3 revenue was 4.2 million dollars, "
    b"up from 3.1 million the prior quarter. "
    b"The marketing section notes a 15 percent budget reduction in September, "
    b"attributed to the Project Phoenix launch on October 3rd, 2025. "
    b"Closing remarks and forward-looking statements follow here."
)


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("FENRIS_HOME", str(tmp_path / "home"))
    cfg = FenrisConfig(data_dir=tmp_path / "home")
    conn = connect(cfg.db_path)
    meta = FileMeta(drive_id="mydrive", file_id="f1", title="Q3 Report.txt",
                    mime_type="text/plain", source_revision="m1")
    ingest_file(conn, None, meta, BODY)
    conn.execute(
        "INSERT INTO drive_files(drive_id, file_id, title, drive_version, updated_at)"
        " VALUES ('mydrive','f1','Q3 Report.txt','1','t')")
    conn.commit()
    chunk_id = conn.execute("SELECT chunk_id FROM chunks LIMIT 1").fetchone()[0]
    conn.close()

    from fenris.api import app

    return TestClient(app), chunk_id


def test_source_view_resolves(seeded):
    tc, chunk_id = seeded
    r = tc.get(f"/api/source-view?chunk_id={chunk_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Q3 Report.txt"
    assert body["cited"]["chunk_id"] == chunk_id
    assert isinstance(body["chunks"], list) and len(body["chunks"]) >= 1
    # the cited chunk's own text is present in the neighbor list
    ids = [c["chunk_id"] for c in body["chunks"]]
    assert chunk_id in ids


def test_source_view_retired_revision_404s(seeded):
    """A tombstoned source must not preview - the honest answer is gone."""
    tc, chunk_id = seeded
    from fenris.config import FenrisConfig
    from fenris.db import connect as db_connect

    cfg = FenrisConfig()
    conn = db_connect(cfg.db_path)
    conn.execute("UPDATE sources SET active = 0 WHERE file_id='f1'")
    conn.commit()
    conn.close()
    r = tc.get(f"/api/source-view?chunk_id={chunk_id}")
    assert r.status_code == 404


def test_source_view_missing_chunk_404s(seeded):
    tc, _ = seeded
    assert tc.get("/api/source-view?chunk_id=99999").status_code == 404


def test_citations_now_carry_viewer_offsets(seeded, tmp_path):
    """The Citation payload includes chunk_id + span for the jump - populated
    only by the renderer (from retrieved evidence), never from model output.

    Tested at the renderer level: the full ask() ladder correctly resolves
    INSUFFICIENT for a corpus this small and withholds citations, so driving
    the ladder here would assert against a different property.
    """
    from fenris.config import FenrisConfig
    from fenris.db import connect as db_connect
    from fenris.generate.generator import FakeGenerator
    from fenris.generate.render import render_answer
    from fenris.retrieve import search

    cfg = FenrisConfig()
    conn = db_connect(cfg.db_path)
    ev_set = search(conn, "revenue", None)
    evidence = ev_set.evidence if hasattr(ev_set, "evidence") else ev_set
    if not evidence:
        pytest.skip("no evidence for the probe query")
    ev = evidence[0]
    gen = FakeGenerator(outputs=[{
        "state": "ANSWERABLE",
        "answer": f"4.2 million [E:{ev.evidence_id}]",
        "cited_evidence_ids": [ev.evidence_id],
        "claims_conflict": False, "missing_evidence": [],
    }])
    rendered = render_answer(gen.generate("p"), evidence, conn=conn)
    conn.close()
    assert rendered.citations, "the renderer resolves real evidence ids"
    c = rendered.citations[0]
    assert c.chunk_id is not None
    assert c.file_id == "f1"
