"""Related-files tests: co-citation, vector affinity, and the fusion.

The seeded conversation turn is inserted through the API itself
(the real write path) instead of hand-written INSERTs, keeping the
test free of any SQL string the scanner must reason about.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fenris.config import FenrisConfig
from fenris.db import connect
from fenris.ingest.pipeline import FileMeta, ingest_file


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("FENRIS_HOME", str(tmp_path / "home"))
    cfg = FenrisConfig(data_dir=tmp_path / "home")
    conn = connect(cfg.db_path)

    ingest_file(conn, None, FileMeta(
        drive_id="mydrive", file_id="taxes", title="Taxes 2024",
        mime_type="text/plain", source_revision="r1"),
        b"property tax assessment and deduction rules")
    ingest_file(conn, None, FileMeta(
        drive_id="mydrive", file_id="house", title="House repairs",
        mime_type="text/plain", source_revision="r2"),
        b"roof repair invoice for the residence property")
    ingest_file(conn, None, FileMeta(
        drive_id="mydrive", file_id="recipes", title="Recipes",
        mime_type="text/plain", source_revision="r3"),
        b"banana bread recipe with thyme and basil")
    conn.commit()
    conn.close()

    from fenris.api import app

    tc = TestClient(app)

    # a stored conversation turn co-citing taxes + house, written
    # through the same API the chat page uses
    cid = tc.post("/api/conversations", json={"title": "tax chat"}).json()["id"]
    tc.post(f"/api/conversations/{cid}/turns", json={
        "question": "tax and repair rules?",
        "state": "ANSWERABLE",
        "answer": "the rules",
        "citations_json": (
            '[{"evidence_id":"x","title":"Taxes 2024","page":1,'
            '"quote":"q","revision_date":null,"file_id":"taxes"},'
            '{"evidence_id":"y","title":"House repairs","page":1,'
            '"quote":"q","revision_date":null,"file_id":"house"}]'
        ),
    })
    return tc


def test_related_via_co_citation(seeded):
    r = seeded.get("/api/files/mydrive/taxes/related")
    assert r.status_code == 200
    related = r.json()["related"]
    ids = {x["file_id"] for x in related}
    assert "house" in ids          # co-cited with taxes
    assert "taxes" not in ids       # never itself
    reasons = {x["file_id"]: x["reason"] for x in related}
    assert reasons["house"] in ("co-cited", "co-cited+similar")


def test_related_via_vector_affinity_valid_shape(seeded):
    # recipes has no co-citation history; the endpoint must still answer
    # with a valid, self-excluding, reason-tagged list
    r = seeded.get("/api/files/mydrive/recipes/related")
    assert r.status_code == 200
    for x in r.json()["related"]:
        assert x["file_id"] != "recipes"
        assert x["reason"] in ("co-cited", "similar", "co-cited+similar")
        assert isinstance(x["score"], float)
        assert x["title"]


def test_related_unknown_file_empty(seeded):
    r = seeded.get("/api/files/mydrive/no-such/related")
    assert r.status_code == 200
    assert r.json()["related"] == []


def test_fusion_scores_sorted(seeded):
    from fenris.config import FenrisConfig
    from fenris.db import connect as db_connect
    from fenris.related import related_files

    conn = db_connect(FenrisConfig().db_path)
    out = related_files(conn, "taxes")
    conn.close()
    if len(out) >= 2:
        assert out[0].score >= out[1].score
    assert all(isinstance(r.title, str) and r.title for r in out)
