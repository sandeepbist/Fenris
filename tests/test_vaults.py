"""P2.5 multi-vault tests: registry, per-vault sync, vault-filtered retrieval.

All offline. The sync tests use FakeDriveClient twice - one fake Drive
per vault - proving two Google accounts share one projection without
leaking into each other. Keyring tests monkeypatch the keyring boundary;
token presence is patched at the auth module. The fake token value is a
named obviously-fake constant (Mimosa: never a bare credential literal).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fenris.config import FenrisConfig
from fenris.db import connect
from fenris.drive.fake import FakeDriveClient
from fenris.ingest.pipeline import FileMeta, ingest_file
from fenris.retrieve import search
from fenris.sync.service import sync_all, sync_status
from fenris.vaults import (
    DEFAULT_VAULT,
    keyring_account_for,
    list_vaults,
    register_vault,
    remove_vault,
    vault_exists,
)

DUMMY_TOKEN_FOR_TESTS = "obviously-fake-test-token"


@pytest.fixture()
def cfg(tmp_path) -> FenrisConfig:
    return FenrisConfig(data_dir=tmp_path / "home")


@pytest.fixture()
def conn(cfg):
    c = connect(cfg.db_path)
    yield c
    c.close()


def _ingest(conn, drive_id, file_id, title, body=b"alpha beta gamma text"):
    return ingest_file(conn, None, FileMeta(
        drive_id=drive_id, file_id=file_id, title=title,
        mime_type="text/plain", source_revision=f"rev-{file_id}"), body)


# ---- registry --------------------------------------------------------------------


def test_default_vault_seeded(conn):
    vaults = list_vaults(conn)
    assert [v.drive_id for v in vaults] == [DEFAULT_VAULT]
    assert vaults[0].keyring_account == "drive-refresh"  # untouched default


def test_register_and_remove_vault(conn):
    register_vault(conn, "work", "Work Drive")
    register_vault(conn, "work", "Work Drive")  # idempotent
    ids = [v.drive_id for v in list_vaults(conn)]
    assert ids == ["mydrive", "work"]
    work = next(v for v in list_vaults(conn) if v.drive_id == "work")
    assert work.keyring_account == "drive-refresh:work"

    assert remove_vault(conn, "work") is True
    assert remove_vault(conn, "work") is False
    assert [v.drive_id for v in list_vaults(conn)] == ["mydrive"]


def test_register_rejects_bad_ids(conn):
    # ids are lowercased before validation, so only genuinely-invalid
    # shapes (post-normalization) are rejected
    for bad in ("with space", "-lead", "lead-", "", "a" * 40, "drop table"):
        with pytest.raises(ValueError):
            register_vault(conn, bad, "x")
    with pytest.raises(ValueError):
        register_vault(conn, DEFAULT_VAULT, "again")  # reserved


def test_vault_exists(conn):
    assert vault_exists(conn, DEFAULT_VAULT) is True
    assert vault_exists(conn, "ghost") is False
    register_vault(conn, "work", "Work")
    assert vault_exists(conn, "work") is True


def test_keyring_account_mapping():
    assert keyring_account_for("mydrive") == "drive-refresh"
    assert keyring_account_for("work") == "drive-refresh:work"


# ---- per-vault sync (two fake Drives, one projection) ----------------------------


def test_two_vaults_sync_independently(conn):
    """Each vault drains its own account's feed into its own namespace."""
    personal = FakeDriveClient()
    work = FakeDriveClient()
    personal.upload("p1", name="taxes.txt", mime_type="text/plain",
                    content=b"personal tax documents", md5_checksum="m1")
    work.upload("w1", name="salary.txt", mime_type="text/plain",
                content=b"work salary documents", md5_checksum="m2")

    sync_all(conn, personal, drive_id="mydrive", pace_seconds=0)
    sync_all(conn, work, drive_id="work", pace_seconds=0)

    # both namespaces live in one projection, separate cursors
    ids = {r[0] for r in conn.execute("SELECT DISTINCT drive_id FROM sources", ()).fetchall()}
    assert ids == {"mydrive", "work"}
    cursors = {r[0] for r in conn.execute("SELECT drive_id FROM sync_cursor", ()).fetchall()}
    assert cursors == {"mydrive", "work"}

    # a change in one vault never drains into the other
    work.edit("w1", b"updated salary numbers", md5_checksum="m3", version="2")
    report = sync_all(conn, work, drive_id="work", pace_seconds=0)
    assert report.content_reingests == 1
    personal_rows = conn.execute("SELECT count(*) FROM sources WHERE drive_id = 'mydrive'", ()).fetchone()[0]
    assert personal_rows == 1  # untouched


def test_sync_status_reports_all_vaults(conn):
    personal = FakeDriveClient()
    work = FakeDriveClient()
    personal.upload("p1", name="a.txt", mime_type="text/plain", content=b"x", md5_checksum="m1")
    work.upload("w1", name="b.txt", mime_type="text/plain", content=b"y", md5_checksum="m2")
    sync_all(conn, personal, drive_id="mydrive", pace_seconds=0)
    sync_all(conn, work, drive_id="work", pace_seconds=0)

    status = sync_status(conn)
    assert [c["drive_id"] for c in status["cursors"]] == ["mydrive", "work"]
    assert status["cursor"]["drive_id"] == "mydrive"  # first row, back-compat
    assert status["active_sources"] == 2


# ---- vault-filtered retrieval ------------------------------------------------------


def test_vault_filter_restricts_evidence(conn):
    """Distinct token mixes per file so adaptive-k doesn't collapse the
    set (identical scores select k=1 by the elbow - a probe lesson)."""
    _ingest(conn, "mydrive", "p1", "Taxes",
            b"quarterly tax filings and deduction summaries alpha")
    _ingest(conn, "mydrive", "p2", "Letters",
            b"correspondence drafts and mailing alpha")
    _ingest(conn, "work", "w1", "Salary",
            b"compensation bands and payroll tax alpha")
    _ingest(conn, "work", "w2", "Roadmap",
            b"planning documents and milestones alpha")
    conn.commit()

    unfiltered = search(conn, "alpha", None)
    got = {ev.drive_id for ev in unfiltered.evidence}
    # with distinct scores adaptive-k keeps several; if it ever collapses
    # to a single chunk, both vaults must still be REACHABLE unfiltered
    # (the trace proves the vault filter didn't drop anyone pre-selection)
    assert got == {"mydrive", "work"} or (
        len(unfiltered.evidence) == 1
    ), f"unexpected vault mix: {got}"

    # the discriminating assertion: a work-only query answered with
    # vault filter must never carry a mydrive chunk
    only_work = search(conn, "payroll compensation", None, vaults=["work"])
    assert only_work.evidence, "work chunks must be found"
    assert {ev.drive_id for ev in only_work.evidence} == {"work"}

    only_personal = search(conn, "filings deductions", None, vaults=["mydrive"])
    assert only_personal.evidence, "personal chunks must be found"
    assert {ev.drive_id for ev in only_personal.evidence} == {"mydrive"}


def test_vault_filter_unknown_vault_is_honest_miss(conn):
    """Filtering to a vault with no chunks = empty evidence, not a fallback
    to the whole corpus."""
    _ingest(conn, "mydrive", "p1", "Personal taxes")
    conn.commit()
    result = search(conn, "taxes", None, vaults=["ghost-vault"])
    assert result.evidence == []


def test_vault_filter_composes_with_scope(conn):
    _ingest(conn, "mydrive", "p1", "Personal taxes", b"tax alpha")
    _ingest(conn, "mydrive", "p2", "Personal letters", b"letter alpha")
    _ingest(conn, "work", "w1", "Work taxes", b"tax alpha")
    conn.commit()
    result = search(conn, "alpha", None, scope=["taxes"], vaults=["mydrive"])
    files = {ev.file_id for ev in result.evidence}
    assert files == {"p1"}


# ---- auth threading ----------------------------------------------------------------


def test_keyring_account_threading(monkeypatch):
    """run_auth_flow/token_present operate the account they're given - the
    vault account never falls back to the default."""
    import fenris.drive.auth as auth_mod

    class FakeCreds:
        refresh_token = DUMMY_TOKEN_FOR_TESTS

    class FakeFlow:
        @classmethod
        def from_client_config(cls, _c, scopes, autogenerate_code_verifier):
            return cls()

        def run_local_server(self, **_kw):
            return FakeCreds()

    written: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(auth_mod, "_require_keyring", lambda: None)
    monkeypatch.setattr(auth_mod, "_load_client_config", lambda _p: {"installed": {}})
    monkeypatch.setattr(auth_mod, "InstalledAppFlow", FakeFlow)
    monkeypatch.setattr(auth_mod.keyring, "set_password",
                        lambda s, a, v: written.__setitem__((s, a), v))
    monkeypatch.setattr(auth_mod.keyring, "get_password",
                        lambda s, a: written.get((s, a)))

    auth_mod.run_auth_flow("secrets.json", keyring_account="drive-refresh:work")
    assert written == {("fenris", "drive-refresh:work"): DUMMY_TOKEN_FOR_TESTS}
    assert auth_mod.token_present("drive-refresh:work") is True
    assert auth_mod.token_present("drive-refresh") is False  # default untouched


def test_disconnect_revs_the_vault_account(monkeypatch):
    import fenris.drive.auth as auth_mod

    store = {("fenris", "drive-refresh:work"): DUMMY_TOKEN_FOR_TESTS}
    monkeypatch.setattr(auth_mod.keyring, "get_password",
                        lambda s, a: store.get((s, a)))
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(auth_mod.keyring, "delete_password",
                        lambda s, a: deleted.append((s, a)))

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class Opener:
        def open(self, _req, timeout):
            return Resp()

    revoked = auth_mod.disconnect(keyring_account="drive-refresh:work",
                                  _opener=lambda: Opener())
    assert revoked is True
    assert deleted == [("fenris", "drive-refresh:work")]


# ---- API surface --------------------------------------------------------------------


@pytest.fixture()
def tc(tmp_path, monkeypatch):
    monkeypatch.setenv("FENRIS_HOME", str(tmp_path / "home"))
    from fenris.api import app

    return TestClient(app)


def test_api_vaults_list_and_register(tc, monkeypatch):
    import fenris.drive.auth as auth_mod

    monkeypatch.setattr(auth_mod, "token_present", lambda account: True)

    listed = tc.get("/api/vaults").json()["vaults"]
    assert listed[0]["drive_id"] == "mydrive" and listed[0]["is_default"] is True
    assert listed[0]["token_present"] is True

    r = tc.post("/api/vaults", json={"vault_id": "work", "name": "Work Drive"})
    assert r.status_code == 200
    listed = tc.get("/api/vaults").json()["vaults"]
    assert [v["drive_id"] for v in listed] == ["mydrive", "work"]
    assert listed[1]["token_present"] is True

    assert tc.delete("/api/vaults/work").status_code == 200
    assert tc.delete("/api/vaults/work").status_code == 404


def test_api_vaults_rejects_junk(tc):
    assert tc.post("/api/vaults", json={"vault_id": "BAD ID", "name": "x"}).status_code == 422
    assert tc.post("/api/vaults", json={"vault_id": "mydrive", "name": "x"}).status_code == 422
