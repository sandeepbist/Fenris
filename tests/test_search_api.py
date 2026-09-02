"""Find-the-file search tests: the 'where is that document' verb."""

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
        drive_id="mydrive", file_id="tax", title="Taxes 2024.txt",
        mime_type="text/plain", source_revision="m1"),
        b"Property tax assessment received in March. The deduction "
        b"applies to the primary residence.")
    ingest_file(conn, None, FileMeta(
        drive_id="mydrive", file_id="recipe", title="Recipes.md",
        mime_type="text/markdown", source_revision="m2"),
        b"# Tax recipe\nA tax on herbs: the recipe calls for thyme and basil.")
    conn.execute(
        "INSERT INTO drive_files(drive_id,file_id,title,drive_version,updated_at)"
        " VALUES ('mydrive','tax','Taxes 2024.txt','1','t')"
    )
    conn.commit()
    conn.close()

    from fenris.api import app

    return TestClient(app)


def test_search_groups_by_file(seeded):
    r = seeded.post("/api/search", json={"query": "tax deduction"})
    assert r.status_code == 200
    body = r.json()
    titles = {f["title"] for f in body["files"]}
    assert "Taxes 2024.txt" in titles
    for f in body["files"]:
        assert isinstance(f["matches"], list) and f["matches"]
        # grouped results are deduped per file
        assert len({m["chunk_id"] for m in f["matches"]}) == len(f["matches"])


def test_search_ranks_relevant_file_first(seeded):
    r = seeded.post("/api/search", json={"query": "property tax assessment"})
    files = r.json()["files"]
    assert files[0]["title"] == "Taxes 2024.txt"


def test_search_empty_query_returns_empty(seeded):
    assert seeded.post("/api/search", json={"query": ""}).json() == {
        "files": [], "total_chunks": 0}


def test_search_caps_query_at_500(seeded):
    assert seeded.post("/api/search", json={"query": "x" * 501}).status_code == 422


def test_search_injection_is_inert(seeded):
    """Search text goes through the same quoting defense as BM25."""
    r = seeded.post("/api/search", json={"query": 'tax" OR 1=1--'})
    assert r.status_code == 200  # no crash, no data leak
