"""Doctor: one pass over everything that can break Fenris, plainly reported.

Each check returns (name, ok, detail). The CLI prints a table; the API
serves the same list for the Settings page. Checks are ordered by how
early they block usage: engine stores first, then credentials, then
Drive, then index health.
"""

from __future__ import annotations

import sqlite3


def _check_data_dir() -> dict:
    from fenris.config import load_config

    cfg = load_config()
    db = cfg.db_path
    if not db.exists():
        return {"name": "data directory", "ok": False,
                "detail": f"no database at {db} - run `fenris init`"}
    return {"name": "data directory", "ok": True, "detail": str(db)}


def _check_schema() -> dict:
    from fenris.config import load_config
    from fenris.db import connect

    cfg = load_config()
    if not cfg.db_path.exists():
        return {"name": "schema", "ok": False, "detail": "no database yet"}
    try:
        conn = connect(cfg.db_path)
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            required = {"sources", "chunks", "chunks_fts", "sync_cursor",
                        "conversations", "turns"}
            missing = required - tables
            if missing:
                return {"name": "schema", "ok": False,
                        "detail": f"missing tables: {sorted(missing)}"}
            has_vec = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='chunks_vec'").fetchone())
            if not has_vec:
                return {"name": "schema", "ok": False,
                        "detail": "vec0 missing - sqlite-vec failed to load"}
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"name": "schema", "ok": False, "detail": f"unreadable: {e}"}
    return {"name": "schema", "ok": True, "detail": "all tables present, vec0 loaded"}


def _check_keyring() -> dict:
    import keyring
    from keyring.backends import fail as fail_backend
    from keyring.backends import null as null_backend

    backend = keyring.get_keyring()
    if isinstance(backend, (null_backend.Keyring, fail_backend.Keyring)):
        return {"name": "OS keyring", "ok": False,
                "detail": "no usable backend - install/unlock gnome-keyring "
                          "(Linux), or run inside a desktop session"}
    return {"name": "OS keyring", "ok": True,
            "detail": type(backend).__name__}


def _check_api_key() -> dict:
    import os

    if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        return {"name": "Gemini API key", "ok": True,
                "detail": "set in environment"}
    return {"name": "Gemini API key", "ok": False,
            "detail": "GOOGLE_API_KEY / GEMINI_API_KEY not set - "
                      "get one at aistudio.google.com/apikey"}


def _check_drive_auth() -> dict:
    try:
        from fenris.drive.auth import token_present

        if token_present():
            return {"name": "Drive connection", "ok": True,
                    "detail": "refresh token stored (read-only scope)"}
        return {"name": "Drive connection", "ok": False,
                "detail": "not connected - run `fenris drive auth`"}
    except Exception as e:  # noqa: BLE001 - keyring backends raise varied types
        return {"name": "Drive connection", "ok": False, "detail": str(e)[:120]}


def _check_index() -> dict:
    from fenris.config import load_config
    from fenris.db import connect

    cfg = load_config()
    if not cfg.db_path.exists():
        return {"name": "index", "ok": False, "detail": "no database yet"}
    conn = connect(cfg.db_path)
    try:
        active = conn.execute(
            "SELECT count(*) FROM sources WHERE active=1").fetchone()[0]
        chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        if active == 0:
            return {"name": "index", "ok": False,
                    "detail": "empty - run `fenris sync run` to index your Drive"}
        return {"name": "index", "ok": True,
                "detail": f"{active} active sources, {chunks} chunks"}
    finally:
        conn.close()


def _check_disk() -> dict:
    import shutil

    from fenris.config import load_config

    cfg = load_config()
    free = shutil.disk_usage(str(cfg.data_dir)).free
    gb = free / (1024 ** 3)
    if gb < 1:
        return {"name": "disk space", "ok": False,
                "detail": f"{gb:.1f} GB free at {cfg.data_dir} - dangerously low"}
    return {"name": "disk space", "ok": True, "detail": f"{gb:.0f} GB free"}


def _check_local_models() -> dict:
    """Optional capability: never a failure, always informative."""
    from fenris.local_models import local_available

    report = local_available()
    parts = []
    if report["embedder"]:
        parts.append("embeddings ok")
    if report["generator"]:
        parts.append("answers ok")
    if parts:
        return {"name": "local models (optional)", "ok": True,
                "detail": "offline mode available: " + ", ".join(parts)}
    return {"name": "local models (optional)", "ok": True,
            "detail": "not installed - online-only "
                      "(pip install fastembed llama-cpp-python + model weights)"}


_CHECKS = [
    _check_data_dir,
    _check_schema,
    _check_keyring,
    _check_api_key,
    _check_drive_auth,
    _check_index,
    _check_disk,
    _check_local_models,
]


def run_doctor() -> list[dict]:
    """Every check, each isolated: one failure never hides the others."""
    out = []
    for check in _CHECKS:
        try:
            out.append(check())
        except Exception as e:  # noqa: BLE001 - a crashing check is a finding
            out.append({"name": check.__name__.replace("_check_", ""),
                        "ok": False, "detail": f"check crashed: {type(e).__name__}"})
    return out


def all_ok(results: list[dict]) -> bool:
    return all(r["ok"] for r in results)
