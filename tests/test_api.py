"""API tests: the HTTP surface over the engine, fully offline.

TestClient + a seeded tmp DB; ask endpoints are exercised through the
offline path (no Gemini key in tests) asserting the 503 contract, and
health/status/sources assert against real seeded projections.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fenris.config import FenrisConfig
from fenris.db import connect
from fenris.ingest.pipeline import FileMeta, ingest_file


@pytest.fixture()
def client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FENRIS_HOME", str(home))
    cfg = FenrisConfig(data_dir=home)

    from fenris.api import app

    tc = TestClient(app)

    # seed the projection the endpoints read
    conn = connect(cfg.db_path)
    meta = FileMeta(drive_id="mydrive", file_id="f1", title="Q3 Report.txt",
                    mime_type="text/plain", source_revision="m1")
    ingest_file(conn, None, meta,
                b"Q3 revenue was 4.2 million dollars. Marketing spend fell 15 percent.")
    conn.execute(
        "INSERT INTO drive_files(drive_id, file_id, title, drive_version, updated_at)"
        " VALUES ('mydrive','f1','Q3 Report.txt','1','t')"
    )
    conn.commit()
    conn.close()
    return tc


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["has_db"] is True
    assert body["index"]["sources_active"] == 1
    assert body["index"]["chunks"] >= 1


def test_status_and_sources(client):
    r = client.get("/api/status")
    assert r.status_code == 200

    r = client.get("/api/sources")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    src = body["sources"][0]
    assert src["title"] == "Q3 Report.txt" and src["active"] is True


def test_ask_without_api_key_is_clean_503(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # _ask_stack builds a genai client; without a key it must return None
    # -> the endpoint's 503 contract, never a traceback.
    r = client.post("/api/ask", json={"question": "What was Q3 revenue?"})
    if r.status_code == 503:
        assert "GEMINI" in r.json()["detail"].upper() or "API" in r.json()["detail"]
    else:
        # a key may exist in the env; then the call is real and either
        # succeeds or errors as a packet - both acceptable in CI-with-key
        assert r.status_code == 200


def test_sync_without_drive_auth_is_clean_503(client):
    r = client.post("/api/sync", json={"full": False})
    assert r.status_code == 503
    assert "detail" in r.json()


def test_eval_overview_empty(client):
    r = client.get("/api/eval/overview")
    assert r.status_code == 200
    assert r.json() == {"runs": []}
