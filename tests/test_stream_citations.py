"""Mid-stream citation tests: live evidence anchors before the answer ends.

The contract (P2.3): while text streams, any [E:id] marker that
resolves against the retrieved set emits ('citation', {...}) BEFORE
the final done event - the UI can anchor citations while the answer
is still writing. Unresolvable ids never emit.

Corpus note: BM25 on this tiny corpus ANDs tokens, so the probe and
the ask use the same single-token query ("revenue") that reliably
retrieves the seeded evidence.
"""

from __future__ import annotations

import json

import pytest

from fenris.config import FenrisConfig
from fenris.db import connect
from fenris.generate.generator import FakeGenerator
from fenris.generate.service import ask_stream
from fenris.ingest.pipeline import FileMeta, ingest_file
from fenris.retrieve import search


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """One seeded DB shared by the module: ingest once, reuse the conn.

    Yields (conn, db_path) - the API-layer test opens its own
    thread-tolerant connection to the same db_path.
    """
    home = tmp_path_factory.mktemp("home")
    cfg = FenrisConfig(data_dir=home)
    conn = connect(cfg.db_path)
    ingest_file(conn, None, FileMeta(
        drive_id="mydrive", file_id="f1", title="Q3 Report",
        mime_type="text/plain", source_revision="m1"),
        b"Q3 revenue was 4.2 million dollars. Marketing spend fell.")
    yield conn, cfg.db_path
    conn.close()


def _run_stream(conn, marker_id: str, answer: str):
    gen = FakeGenerator(outputs=[{
        "state": "ANSWERABLE",
        "answer": answer,
        "cited_evidence_ids": [marker_id],
        "claims_conflict": False, "missing_evidence": [],
    }])
    return list(ask_stream(
        conn, "revenue", None, generator=gen,
        search_fn=lambda q: search(conn, q, None)))


def test_resolved_citation_emits_mid_stream(corpus):
    conn, _db_path = corpus
    ev = search(conn, "revenue", None)
    evidence = ev.evidence if hasattr(ev, "evidence") else ev
    assert evidence, "probe must retrieve before the stream can cite"
    real_id = evidence[0].evidence_id

    items = _run_stream(conn, real_id, f"Revenue was 4.2 million [E:{real_id}].")
    citations = [i for i in items if isinstance(i, tuple) and i[0] == "citation"]
    packets = [i for i in items if not isinstance(i, (str, tuple))]

    assert citations, "a resolvable [E:id] must emit a citation event"
    assert packets, "the stream must end with the AnswerPacket"
    assert items.index(citations[0]) < items.index(packets[-1])

    payload = citations[0][1]
    assert payload["evidence_id"] == real_id
    assert "title" in payload and "chunk_id" in payload


def test_unresolvable_id_never_emits(corpus):
    conn, _db_path = corpus
    items = _run_stream(conn, "deadbeefdeadbeef",
                        "Trust nothing [E:deadbeefdeadbeef].")
    citations = [i for i in items if isinstance(i, tuple) and i[0] == "citation"]
    ids_emitted = {c[1]["evidence_id"] for c in citations}
    assert "deadbeefdeadbeef" not in ids_emitted


def test_citation_events_deduplicated(corpus):
    conn, _db_path = corpus
    ev = search(conn, "revenue", None)
    evidence = ev.evidence if hasattr(ev, "evidence") else ev
    real_id = evidence[0].evidence_id
    # the id appears in the answer AND again in the JSON tail the fake
    # streams - the rescan must emit it exactly once
    items = _run_stream(conn, real_id, f"Revenue [E:{real_id}] plus [E:{real_id}] again.")
    for_id = [i for i in items
              if isinstance(i, tuple) and i[1]["evidence_id"] == real_id]
    assert len(for_id) == 1


def test_api_stream_carries_citation_events(corpus, tmp_path, monkeypatch):
    _conn, db_path = corpus
    """The SSE wire carries {'type': 'citation', ...} events before done.

    The endpoint's own _db() opens its conn from FENRIS_HOME, so
    FENRIS_HOME must point AT THE CORPUS DB's home (not a fresh one) -
    the stack monkeypatch only replaces the embedder+generator, not
    the connection retrieval runs against.
    """
    from pathlib import Path as _Path

    from fastapi.testclient import TestClient

    # FENRIS_HOME = the corpus db's parent directory
    monkeypatch.setenv("FENRIS_HOME", str(_Path(db_path).parent))

    def fake_stack(c, prefer_local=False):
        from fenris.ingest.embed import FakeEmbedder

        # probe with the SAME shape the endpoint's search_fn uses
        # (FakeEmbedder qvec + full hybrid search), so the id baked
        # into the generator's answer is the one retrieval will keep
        emb = FakeEmbedder()
        qvec = emb.embed_documents(["task: search result | query: revenue"])[0]
        ev = search(c, "revenue", qvec)
        evidence = ev.evidence if hasattr(ev, "evidence") else ev
        real_id = evidence[0].evidence_id if evidence else "0" * 16
        gen = FakeGenerator(outputs=[{
            "state": "ANSWERABLE",
            "answer": f"Revenue was 4.2 million [E:{real_id}].",
            "cited_evidence_ids": [real_id],
            "claims_conflict": False, "missing_evidence": [],
        }])
        return emb, gen

    monkeypatch.setattr("fenris.api._ask_stack", fake_stack)
    from fenris.api import app

    tc = TestClient(app)
    with tc.stream("POST", "/api/ask/stream",
                   json={"question": "revenue"}) as response:
        raw = "".join(chunk for chunk in response.iter_text())
    events = []
    for block in raw.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    types = [e["type"] for e in events]
    assert "citation" in types
    assert types.index("citation") < types.index("done")
    assert events[types.index("citation")]["evidence_id"]
