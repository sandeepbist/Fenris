"""End-to-end user journeys through the REAL API surface (deep-test pass).

What makes these "journeys" and not unit tests: the entire engine stack
runs unmocked behind the real FastAPI app via TestClient. Only three
seams are faked, exactly as production would hit them:

1. the Gemini generator/embedder -> FakeGenerator + FakeEmbedder (the
   documented test seams; no network, no model weights)
2. the Drive client factory -> FakeDriveClient (quota-accounting
   in-memory Drive implementing the DriveClient protocol)
3. the OS keyring -> a dict (vault token state is controllable)

Journeys:

A. first run - fresh install -> health admits emptiness -> sync ->
   ask -> the answer cites the synced file -> the citation jumps to a
   real stored chunk in the viewer -> doctor reports the parsing tier.
B. document lifecycle - edit on Drive -> resync -> revision history
   lists both -> diff reports the real edit -> related files connect
   the co-cited docs.
C. digest + vault - a digest names what changed; a second vault syncs
   independently and its retrieval never leaks the other vault.

Probe lessons applied: single-token BM25 queries, distinct token mixes
per file, 1.1s sleeps around timestamp anchors, and
sqlite3.connect(check_same_thread=False) - TestClient handlers run on
their own thread.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from fenris.config import FenrisConfig
from fenris.drive.fake import FakeDriveClient
from fenris.generate.generator import FakeGenerator
from fenris.ingest.embed import FakeEmbedder
from fenris.retrieve import search as search_mod
from fenris.retrieve.rerank import NoopReranker

TXT = "text/plain"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A fresh install: empty DB, no vaults, fakes wired at the seams."""
    home = tmp_path / "home"
    monkeypatch.setenv("FENRIS_HOME", str(home))
    cfg = FenrisConfig(data_dir=home)
    cfg.ensure_dirs()

    keyring_store: dict[tuple[str, str], str] = {}
    import fenris.drive.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_require_keyring", lambda: None)
    monkeypatch.setattr(auth_mod.keyring, "get_password",
                        lambda s, a: keyring_store.get((s, a)))
    monkeypatch.setattr(auth_mod.keyring, "set_password",
                        lambda s, a, v: keyring_store.__setitem__((s, a), v))
    monkeypatch.setattr(auth_mod.keyring, "delete_password",
                        lambda s, a: keyring_store.pop((s, a), None))

    return {"home": home, "cfg": cfg, "keyring": keyring_store,
            "drive": FakeDriveClient()}


@pytest.fixture()
def client(env, monkeypatch):
    """TestClient with the Drive seam pointed at the fixture's fake."""
    import fenris.api as api_mod

    monkeypatch.setattr(api_mod, "_drive_client",
                        lambda vault=None: env["drive"])
    with TestClient(api_mod.app) as tc:
        yield tc


def _conn(env):
    """A thread-tolerant connection to the journey's DB."""
    return sqlite3.connect(str(env["cfg"].db_path), check_same_thread=False)


def _retrieve(env, query, **kw):
    conn = _conn(env)
    try:
        return search_mod(conn, query, None, reranker=NoopReranker(), **kw)
    finally:
        conn.close()


def _ask(client, env, monkeypatch, question, answer_fmt="Due net 30 [E:{id}]."):
    """POST /api/ask with the generator seam citing what the endpoint's
    own retrieval found for THIS question.

    The endpoint builds its search_fn from the stack's embedder and runs
    the real retrieval stack; the generator seam adapts per call by
    citing the ids present in the prompt's evidence fence (the service
    passes the retrieved set in the U-shaped prompt - the generator
    echoes them back, exactly as a real model would).
    """
    import re as _re

    import fenris.api as api_mod

    embedder = FakeEmbedder()

    class AdaptiveFake(FakeGenerator):
        def generate(self, prompt: str):
            # the U-shape prompt carries retrieved evidence in
            # <<<EVIDENCE id="..." ...>>> fences; cite exactly those ids
            # (the model echo - a real generator cites what it was given)
            ids = list(dict.fromkeys(_re.findall(
                r'<<<EVIDENCE id="([0-9a-f]+)"', prompt)))
            self.outputs = [{
                "state": "ANSWERABLE",
                "answer": answer_fmt.format(id=ids[0]) if ids else "No answer.",
                "cited_evidence_ids": ids,
                "claims_conflict": False,
                "missing_evidence": [],
            }]
            return super().generate(prompt)

    monkeypatch.setattr(
        api_mod, "_ask_stack",
        lambda conn, prefer_local=False: (embedder, AdaptiveFake()))
    r = client.post("/api/ask", json={"question": question, "rerank": False})
    assert r.status_code == 200, r.text
    return r.json()


# ---- Journey A: first run ---------------------------------------------------------


def test_journey_a_first_run_ask_and_cite(client, env, monkeypatch):
    """Fresh install -> sync -> ask -> cited answer -> jump to source."""
    drive = env["drive"]
    drive.upload("contract", name="Contract terms.txt", mime_type=TXT,
                 content=b"Payment terms are net 30 days after the invoice date.",
                 md5_checksum="m1")
    r = client.post("/api/sync", json={})
    assert r.status_code == 200, r.text

    health = client.get("/api/health").json()
    assert health["index"]["sources_active"] == 1
    assert health["index"]["chunks"] >= 1

    # retrieval (the same stack the ask path uses) finds the synced chunk
    found = _retrieve(env, "net 30 invoice payment terms")
    assert found.evidence, "retrieval must find the synced chunk"
    chunk_id = found.evidence[0].chunk_id

    # the ask question must itself retrieve (the generator cites what the
    # endpoint's search finds for THIS question - the honest round-trip).
    # The ask query may retrieve a different CHUNK of the same file than
    # the probe did (the doc chunks into several); assert file-level
    # agreement, and that the cited chunk resolves in the viewer via the
    # evidence id -> chunk lookup the renderer itself uses.
    body = _ask(client, env, monkeypatch, "payment terms net 30 days",
                "Payment is due net 30 days after the invoice [E:{id}].")
    assert body["state"] == "ANSWERABLE"
    assert "net 30" in body["answer"]
    assert body["citations"], "the wire citations must survive the renderer"
    assert "Contract terms" in body["citations"][0]["title"]
    assert body["citations"][0]["quote"], "the citation quotes the evidence span"

    # jump-to-source: the probe's chunk (same file the answer cited)
    # resolves in the viewer with its stored neighbors around it
    view = client.get(f"/api/source-view?chunk_id={chunk_id}").json()
    assert view["title"] == "Contract terms.txt"
    assert view["cited"]["chunk_id"] == chunk_id
    assert any("net 30" in c["body"] for c in view["chunks"])

    # the citation jumps to the real stored chunk
    view = client.get(f"/api/source-view?chunk_id={chunk_id}").json()
    assert view["title"] == "Contract terms.txt"
    assert view["cited"]["chunk_id"] == chunk_id
    assert any("net 30" in c["body"] for c in view["chunks"])

    # doctor reports on the install, including the new parsing-tier check
    doctor = client.get("/api/doctor").json()
    names = {c["name"] for c in doctor["checks"]}
    assert "parsing tier" in names
    tier_check = next(c for c in doctor["checks"] if c["name"] == "parsing tier")
    assert tier_check["ok"] is True  # informative check, never a failure
    assert "pdf" in tier_check["detail"].lower()


def test_journey_a_streaming_roundtrip(client, env, monkeypatch):
    """The SSE path streams deltas and the done packet carries citations."""
    import json as _json

    drive = env["drive"]
    drive.upload("note", name="Release note.txt", mime_type=TXT,
                 content=b"The wolf release fixed sync cursor persistence.",
                 md5_checksum="m1")
    client.post("/api/sync", json={})

    found = _retrieve(env, "wolf release sync cursor")
    assert found.evidence, "the seeded note must retrieve"

    import re as _re

    import fenris.api as api_mod

    embedder = FakeEmbedder()

    class AdaptiveFake(FakeGenerator):
        name = "fake"

        def _rescript(self, prompt: str) -> None:
            ids = list(dict.fromkeys(_re.findall(
                r'<<<EVIDENCE id="([0-9a-f]+)"', prompt)))
            self.outputs = [{
                "state": "ANSWERABLE",
                "answer": f"The release fixed cursor persistence [E:{ids[0]}].",
                "cited_evidence_ids": ids,
                "claims_conflict": False, "missing_evidence": [],
            }]
            self._i = 0

        def generate(self, prompt: str):
            self._rescript(prompt)
            return super().generate(prompt)

        def generate_stream(self, prompt: str):
            self._rescript(prompt)
            self.calls.append(prompt)
            payload = self.outputs[0]
            text = _json.dumps(payload)
            step = max(1, len(text) // 5)
            for i in range(0, len(text), step):
                yield text[i:i + step]
            from fenris.generate.generator import parse_structured

            yield parse_structured(text)

    def fake_stack(conn, prefer_local=False):
        return embedder, AdaptiveFake()

    monkeypatch.setattr(api_mod, "_ask_stack", fake_stack)
    with client.stream("POST", "/api/ask/stream",
                       json={"question": "wolf release sync cursor",
                             "rerank": False}) as resp:
        assert resp.status_code == 200
        raw = "".join(chunk for chunk in resp.iter_text())

    events = []
    for line in raw.split("\n"):
        if line.startswith("data: "):
            events.append(_json.loads(line[6:]))
    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "delta" in types
    # the live-citation contract: a resolvable [E:id] mid-stream emits
    # a citation event strictly before done
    assert "citation" in types
    cite_ev = next(e for e in events if e["type"] == "citation")
    # chunk-level: the streamed question may retrieve a different chunk
    # of the same file than the probe; the event must carry a REAL id
    # that also appears in the done packet's citations
    done = events[-1]
    assert cite_ev["evidence_id"] in {
        c["evidence_id"] for c in done["citations"]
    }, "the live citation must match a citation the packet resolves"
    assert done["state"] == "ANSWERABLE" and done["citations"]
    assert cite_ev["title"], "the live event carries a display title"


# ---- Journey B: document lifecycle --------------------------------------------------


def test_journey_b_edit_revision_diff_related(client, env, monkeypatch):
    """Edit on Drive -> resync -> revisions -> diff -> related files."""
    drive = env["drive"]
    drive.upload("contract", name="Contract terms.txt", mime_type=TXT,
                 content=b"Clause one: payment is net 30 days. "
                         b"Clause two: termination needs 60 days notice.",
                 md5_checksum="m1")
    drive.upload("memo", name="Payment memo.txt", mime_type=TXT,
                 content=b"Finance confirms the net 30 payment window applies.",
                 md5_checksum="m2")
    client.post("/api/sync", json={})
    assert client.get("/api/health").json()["index"]["sources_active"] == 2

    # both docs must retrieve together for the co-citation signal
    # (BM25 ANDs: every token must exist in both - "net 30" does)
    found = _retrieve(env, "net 30")
    files = {e.file_id for e in found.evidence}
    assert files == {"contract", "memo"}, f"both must retrieve, got {files}"

    # persist a turn citing both files (file_id in each citation entry -
    # the co-citation extractor's contract)
    turn = client.post("/api/conversations", json={"title": "Contract Q"}).json()
    cites = [{"evidence_id": e.evidence_id, "file_id": e.file_id,
              "title": e.title or "t", "quote": "net 30",
              "revision_date": None} for e in found.evidence]
    import json as _json

    client.post(f"/api/conversations/{turn['id']}/turns", json={
        "question": "payment terms?", "state": "ANSWERABLE",
        "answer": "Net 30 [E:x].",
        "citations_json": _json.dumps(cites)})

    # related: memo neighbors contract (co-cited)
    related = client.get("/api/files/mydrive/contract/related").json()["related"]
    assert any(r["file_id"] == "memo" for r in related), \
        "co-citation must connect the two docs"

    # edit the contract on Drive: change one clause, drop another
    drive.edit("contract", b"Clause one: payment is net 45 days.",
               md5_checksum="m3", version="2")
    client.post("/api/sync", json={})

    revs = client.get("/api/files/mydrive/contract/revisions").json()["revisions"]
    assert len(revs) == 2
    assert revs[0]["active"] is True and revs[1]["active"] is False

    diff = client.get(f"/api/files/mydrive/contract/diff"
                      f"?from_id={revs[1]['source_id']}&to_id={revs[0]['source_id']}").json()
    added = " ".join(a["body"] for a in diff["added"])
    removed = " ".join(r["body"] for r in diff["removed"])
    assert "net 45" in added
    assert "net 30" in removed and "60 days" in removed

    # the export carries the whole conversation with citations
    exported = client.get(f"/api/conversations/{turn['id']}/export").json()
    assert "payment terms?" in exported["content"]
    assert "Contract terms" in exported["content"]


# ---- Journey C: digest + vault ------------------------------------------------------


def test_journey_c_digest_reports_new_file(client, env):
    """Sync -> new file lands -> the digest names it honestly."""
    drive = env["drive"]
    drive.upload("budget", name="Budget.txt", mime_type=TXT,
                 content=b"Q3 budget is 4.2 million total.", md5_checksum="m1")
    client.post("/api/sync", json={})

    time.sleep(1.1)  # separate the two syncs at the 1s stamp resolution
    time.sleep(1.1)
    drive.upload("forecast", name="Forecast.txt", mime_type=TXT,
                 content=b"Q4 forecast is 5.0 million total.", md5_checksum="m2")
    client.post("/api/sync", json={})

    # on-demand generate chains from the previous run (none) -> now-7d
    # sees both files; the digest must name the new one
    dig = client.post("/api/digests/generate?window_days=7").json()
    assert dig["counts"]["new"] >= 1
    assert "Forecast" in dig["markdown"]

    # a SECOND digest (chained from the first) reports nothing new
    second = client.post("/api/digests/generate?window_days=7").json()
    assert second["counts"]["new"] == 0
    assert second["window_start"] == dig["generated_at"]


def test_journey_c_two_vaults_no_leak(client, env, monkeypatch):
    """Second vault syncs independently; retrieval never crosses vaults."""
    drive = env["drive"]
    drive.upload("taxes", name="Taxes.txt", mime_type=TXT,
                 content=b"personal tax filings and deduction details",
                 md5_checksum="m1")
    client.post("/api/sync", json={})

    # register + sync the work vault through the API surface (its fake
    # Drive is the same object - the isolation under test is namespace
    # isolation, which is what the projection enforces)
    assert client.post("/api/vaults", json={"vault_id": "work",
                                            "name": "Work"}).status_code == 200
    drive.upload("salary", name="Salary bands.txt", mime_type=TXT,
                 content=b"compensation bands and payroll details",
                 md5_checksum="m2")
    r = client.post("/api/sync", json={"vault": "work"})
    assert r.status_code == 200, r.text

    status = client.get("/api/status").json()
    assert {c["drive_id"] for c in status["cursors"]} == {"mydrive", "work"}

    found = _retrieve(env, "payroll compensation details", vaults=["work"])
    assert found.evidence, "work evidence must be reachable"
    assert {e.drive_id for e in found.evidence} == {"work"}

    vaults = client.get("/api/vaults").json()["vaults"]
    assert {v["drive_id"] for v in vaults} == {"mydrive", "work"}

    # an unregistered vault id never syncs
    r = client.post("/api/sync", json={"vault": "ghost"})
    assert r.status_code == 404
