"""Tests for the ops CLI commands: doctor, status, reprocess."""

from __future__ import annotations

from typer.testing import CliRunner

runner = CliRunner()


def _reset_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'c.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)


def test_doctor_runs(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.cli import app
    from localplaud.db.session import init_db

    init_db()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "ffmpeg" in result.stdout
    assert "plaud auth" in result.stdout


def test_status_counts(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope

    init_db()
    with session_scope() as s:
        s.add(PlaudFile(id="a", status=FileStatus.done))
        s.add(PlaudFile(id="b", status=FileStatus.downloaded))

    from localplaud.cli import app

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "done" in result.stdout and "downloaded" in result.stdout


def test_reprocess_missing_file(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.cli import app
    from localplaud.db.session import init_db

    init_db()
    result = runner.invoke(app, ["reprocess", "nope"])
    assert result.exit_code == 1
    assert "no such file" in result.stdout


def test_prepare_independent_command(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.cli import app

    result = runner.invoke(app, ["prepare-independent"])
    assert result.exit_code == 0
    assert "Independent-mode preparation" in result.stdout
