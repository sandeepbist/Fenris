"""Streaming-ask tests: the SSE endpoint's event protocol.

The contract: /api/ask/stream yields `data: {"type": "delta", ...}` lines
while text generates, then a single `data: {"type": "done", ...}` carrying
the full answer payload. Everything must work offline via FakeGenerator.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from fenris.config import FenrisConfig
from fenris.db import connect
from fenris.ingest.pipeline import FileMeta, ingest_file


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FENRIS_HOME", str(tmp_path / "home"))
    cfg = FenrisConfig(data_dir=tmp_path / "home")

    from fenris.api import app

    # Seed through the real pipeline so retrieval finds something.
    conn = connect(cfg.db_path)
    meta = FileMeta(drive_id="mydrive", file_id="f1", title="Q3 Report.txt",
                    mime_type="text/plain", source_revision="m1")
    ingest_file(conn, None, meta,
                b"Q3 revenue was 4.2 million dollars. Marketing spend fell 15 percent.")
    conn.execute(
        "INSERT INTO drive_files(drive_id, file_id, title, drive_version, updated_at)"
        " VALUES ('mydrive','f1','Q3 Report.txt','1','t')")
    conn.commit()
    conn.close()

    monkeypatch.setattr("fenris.api._ask_stack", _fake_stack)
    return TestClient(app)


def _fake_stack(conn, prefer_local: bool = False):
    """Offline ask stack: a scripted FakeGenerator with citation-safe output."""
    from fenris.generate.generator import FakeGenerator
    from fenris.ingest.embed import FakeEmbedder

    gen = FakeGenerator(outputs=[{
        "state": "ANSWERABLE",
        "answer": "Q3 revenue was 4.2 million dollars.",
        "cited_evidence_ids": [],
        "claims_conflict": False,
        "missing_evidence": [],
    }])
    return FakeEmbedder(), gen


def _parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def test_stream_emits_deltas_then_done(client):
    with client.stream(
        "POST", "/api/ask/stream", json={"question": "What was Q3 revenue?"}
    ) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        raw = "".join(chunk for chunk in response.iter_text())
    events = _parse_sse(raw)

    deltas = [e for e in events if e["type"] == "delta"]
    dones = [e for e in events if e["type"] == "done"]
    assert len(deltas) >= 3, "streamed text must arrive in multiple deltas"
    assert len(dones) == 1, "exactly one terminal done event"
    streamed_text = "".join(e["text"] for e in deltas)
    assert "revenue" in streamed_text
    done = dones[0]
    assert done["state"] in ("ANSWERABLE", "INSUFFICIENT")
    assert "citations" in done and "answer" in done


def test_stream_done_payload_matches_non_streaming(client):
    """The stream's final event is the same wire shape as POST /api/ask."""
    r = client.post("/api/ask", json={"question": "What was Q3 revenue?"})
    assert r.status_code == 200
    plain = r.json()

    with client.stream(
        "POST", "/api/ask/stream", json={"question": "What was Q3 revenue?"}
    ) as response:
        raw = "".join(chunk for chunk in response.iter_text())
    done = [e for e in _parse_sse(raw) if e["type"] == "done"][0]

    assert done["state"] == plain["state"]
    assert done["citations"] == plain["citations"]


def test_stream_huge_question_rejected(client):
    r = client.post("/api/ask/stream", json={"question": "x" * 10_001})
    assert r.status_code == 422
