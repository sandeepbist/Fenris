"""Connector tests: the folder connector against real files on disk."""

from __future__ import annotations

import json
import time

import pytest

from fenris.connectors import ChangesPage, ConnectorFile, FolderConnector


@pytest.fixture()
def folder(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.txt").write_text("first note about apples")
    (root / "sub").mkdir()
    (root / "sub" / "b.md").write_text("# second note\nabout bananas")
    (root / "c.pdf").write_bytes(b"%PDF-ignored")   # unsupported: skipped
    (root / "d.bin").write_bytes(b"\x00\x01")       # unsupported: skipped
    return root


def test_list_walks_supported_formats_only(folder):
    conn = FolderConnector(folder)
    ids = [f.file_id for f in conn.list()]
    assert sorted(ids) == ["a.txt", "sub/b.md"]
    mimes = {f.file_id: f.mime_type for f in conn.list()}
    assert mimes["a.txt"] == "text/plain"
    assert mimes["sub/b.md"] == "text/markdown"


def test_fetch_returns_hash_identity(folder):
    conn = FolderConnector(folder)
    f = next(conn.list())
    revision, data = conn.fetch(f)
    assert revision == _sha(b"first note about apples")
    assert data == b"first note about apples"
    # same bytes -> same revision -> upstream dedup works
    revision2, _ = conn.fetch(f)
    assert revision2 == revision


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_changes_detects_edit_and_removal(folder):
    conn = FolderConnector(folder)
    page1 = conn.changes(None)
    assert {f.file_id for f in page1.entries} == {"a.txt", "sub/b.md"}
    token = page1.next_token

    # edit a.txt, delete b.md (sleep past the FS mtime resolution)
    time.sleep(1.1)
    (folder / "a.txt").write_text("first note, edited")
    (folder / "sub" / "b.md").unlink()

    page2 = conn.changes(token)
    assert {f.file_id for f in page2.entries} == {"a.txt"}
    assert page2.removed == ["sub/b.md"]


def test_corrupt_token_degrades_to_full_diff(folder):
    conn = FolderConnector(folder)
    page = conn.changes("not-json{{")
    assert len(page.entries) == 2
    assert page.removed == []


def test_giant_files_skipped(folder):
    conn = FolderConnector(folder)
    big = folder / "huge.txt"
    big.write_bytes(b"x" * (101 * 1024 * 1024))
    try:
        ids = {f.file_id for f in conn.list()}
        assert "huge.txt" not in ids
    finally:
        big.unlink()


def test_source_id_namespaces(folder):
    conn = FolderConnector(folder)
    assert conn.source_id.startswith("folder:")
    custom = FolderConnector(folder, source_id="notes-local")
    assert custom.source_id == "notes-local"


def test_missing_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        FolderConnector(tmp_path / "nope")
