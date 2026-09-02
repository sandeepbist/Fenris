"""Doctor tests: every check isolates, findings are actionable, and the
whole thing never crashes on a broken machine."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FENRIS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_doctor_fresh_machine_reports_failures_with_fixes(home):
    """No DB, no key, no Drive: every failing check says what to do."""
    from fenris.doctor import run_doctor

    results = run_doctor()
    by_name = {r["name"]: r for r in results}
    assert not by_name["data directory"]["ok"]
    assert "fenris init" in by_name["data directory"]["detail"]
    assert not by_name["Gemini API key"]["ok"]
    assert "GOOGLE_API_KEY" in by_name["Gemini API key"]["detail"]
    assert not by_name["Drive connection"]["ok"]
    assert "fenris drive auth" in by_name["Drive connection"]["detail"]


def test_doctor_after_init(home):
    from fenris.config import FenrisConfig
    from fenris.db import connect
    from fenris.doctor import run_doctor

    connect(FenrisConfig(data_dir=home).db_path).close()
    results = run_doctor()
    by_name = {r["name"]: r for r in results}
    assert by_name["data directory"]["ok"]
    assert by_name["schema"]["ok"]


def test_doctor_checks_never_crash_the_run(home, monkeypatch):
    """A crashing check is itself a finding - the run always completes."""
    import fenris.doctor as doctor_mod

    def boom():
        raise RuntimeError("backend exploded")

    # _CHECKS holds direct function refs, so patch the list, not the name
    monkeypatch.setattr(doctor_mod, "_CHECKS", [boom] + doctor_mod._CHECKS[1:])
    results = doctor_mod.run_doctor()
    crashed = [r for r in results if "crashed" in r.get("detail", "")]
    assert len(crashed) == 1 and crashed[0]["ok"] is False


def test_api_doctor_endpoint(home):
    import os

    from fenris.api import app

    os.environ.pop("GOOGLE_API_KEY", None)
    os.environ.pop("GEMINI_API_KEY", None)
    tc = TestClient(app)
    r = tc.get("/api/doctor")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["checks"], list) and len(body["checks"]) >= 5
    assert body["all_ok"] is False


def test_cli_doctor_exit_code(home):
    from typer.testing import CliRunner

    from fenris.cli import app as cli_app

    runner = CliRunner()
    res = runner.invoke(cli_app, ["doctor"])
    assert res.exit_code == 1  # fresh machine: failing checks -> exit 1
    assert "FAIL" in res.output
