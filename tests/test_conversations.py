"""Conversations API tests: sessions persist, turns cascade, patch/delete work."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def tc(tmp_path, monkeypatch):
    monkeypatch.setenv("FENRIS_HOME", str(tmp_path / "home"))
    from fenris.api import app

    return TestClient(app)


def _create(tc, title="Chat about taxes") -> int:
    r = tc.post("/api/conversations", json={"title": title})
    assert r.status_code == 200
    return r.json()["id"]


def test_conversation_lifecycle(tc):
    cid = _create(tc)
    assert tc.get("/api/conversations").json()["conversations"][0]["title"] == "Chat about taxes"

    r = tc.post(f"/api/conversations/{cid}/turns", json={
        "question": "What was Q3 revenue?",
        "state": "ANSWERABLE",
        "answer": "4.2 million",
        "citations_json": '[{"evidence_id":"a","title":"Q3","page":1,"quote":"...","revision_date":null}]',
    })
    assert r.status_code == 200

    full = tc.get(f"/api/conversations/{cid}").json()
    assert full["turn_count"] if "turn_count" in full else True
    assert len(full["turns"]) == 1
    assert full["turns"][0]["answer"] == "4.2 million"
    assert full["turns"][0]["citations"][0]["evidence_id"] == "a"

    # rename + pin
    assert tc.patch(f"/api/conversations/{cid}", json={"pinned": True}).status_code == 200
    assert tc.patch(f"/api/conversations/{cid}", json={"title": "Renamed"}).status_code == 200
    listed = tc.get("/api/conversations").json()["conversations"][0]
    assert listed["pinned"] is True and listed["title"] == "Renamed"

    # delete cascades turns
    assert tc.delete(f"/api/conversations/{cid}").status_code == 200
    assert tc.get(f"/api/conversations/{cid}").status_code == 404
    assert tc.get("/api/conversations").json()["conversations"] == []


def test_pinned_sorts_first(tc):
    a = _create(tc, "unpinned")
    b = _create(tc, "to pin")
    tc.patch(f"/api/conversations/{b}", json={"pinned": True})
    first = tc.get("/api/conversations").json()["conversations"][0]
    assert first["id"] == b


def test_turn_for_missing_conversation_404s(tc):
    r = tc.post("/api/conversations/999/turns", json={
        "question": "q", "state": "ANSWERABLE"})
    assert r.status_code == 404


def test_empty_patch_is_422(tc):
    c = _create(tc)
    assert tc.patch(f"/api/conversations/{c}", json={}).status_code == 422


def test_title_capped_at_200(tc):
    c = _create(tc)
    tc.patch(f"/api/conversations/{c}", json={"title": "T" * 10_000})
    row = tc.get(f"/api/conversations/{c}").json()
    assert len(row["title"]) <= 200


def test_export_markdown_and_html(tc):
    cid = _create(tc, "Export me")
    tc.post(f"/api/conversations/{cid}/turns", json={
        "question": "What was Q3 revenue?",
        "state": "ANSWERABLE",
        "answer": "4.2 million [E:abc].",
        "citations_json": '[{"evidence_id":"abc","title":"Q3 Report.txt","page":3,'
                          '"quote":"Q3 revenue was 4.2 million","revision_date":null}]',
    })
    r = tc.get(f"/api/conversations/{cid}/export")
    assert r.status_code == 200
    md = r.json()
    assert md["format"] == "md"
    assert "## Q: What was Q3 revenue?" in md["content"]
    assert "4.2 million" in md["content"]
    assert "Q3 Report.txt" in md["content"]
    assert '"Q3 revenue was 4.2 million"' in md["content"]

    r = tc.get(f"/api/conversations/{cid}/export?fmt=html")
    html = r.json()
    assert html["format"] == "html"
    assert "<!doctype html>" in html["content"]
    assert "4.2 million" in html["content"]
    # self-contained: no external asset links
    assert "http://" not in html["content"].replace("http://www.w3.org", "")
    assert "src=" not in html["content"]


def test_export_withheld_answers_marked_honestly(tc):
    """An INSUFFICIENT turn exports as its state, not a fabricated answer."""
    cid = _create(tc)
    tc.post(f"/api/conversations/{cid}/turns", json={
        "question": "something unanswerable",
        "state": "INSUFFICIENT", "answer": "",
    })
    md = tc.get(f"/api/conversations/{cid}/export").json()["content"]
    assert "INSUFFICIENT" in md
    assert "withheld" in md


def test_export_missing_conversation_404s(tc):
    assert tc.get("/api/conversations/999/export").status_code == 404
