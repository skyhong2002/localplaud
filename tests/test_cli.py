"""CLI smoke tests via typer's CliRunner — non-blocking commands only."""

from __future__ import annotations

import os

from typer.testing import CliRunner

import localplaud.db.session as db_session
from localplaud.cli import app
from localplaud.config import get_settings

runner = CliRunner()


def _isolate(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("LOCALPLAUD_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    get_settings(reload=True)


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "auth", "poll", "work", "run", "ls", "ask", "serve"):
        assert command in result.output


def test_auth_help_lists_only_official_subcommands():
    result = runner.invoke(app, ["auth", "--help"])
    assert result.exit_code == 0
    assert "check" in result.output and "login" in result.output
    assert "import" not in result.output


def test_init_creates_database(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    db_file = tmp_path / "data" / "cli-test.db"
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{db_file}")
    get_settings(reload=True)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert db_file.exists()
    assert "Database ready" in result.output


def test_auth_check_without_oauth_session_fails_helpfully(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCALPLAUD_PLAUD__OFFICIAL__TOKENS_PATH", str(tmp_path / "tokens.json"))
    get_settings(reload=True)
    result = runner.invoke(app, ["auth", "check"])
    assert result.exit_code != 0
    assert "auth login" in result.output


def test_auth_login_uses_native_pkce_without_node(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    tokens_path = tmp_path / "tokens.json"
    monkeypatch.setenv("LOCALPLAUD_PLAUD__OFFICIAL__TOKENS_PATH", str(tokens_path))
    get_settings(reload=True)
    monkeypatch.setattr(
        "localplaud.plaud.oauth.native_login",
        lambda config, **kwargs: config.tokens_path,
    )
    monkeypatch.setattr("localplaud.cli.auth_check", lambda: None)
    result = runner.invoke(app, ["auth", "login"])
    assert result.exit_code == 0, result.output
    assert "Opening Plaud authorization" in result.output
    assert "Signed in" in result.output
    assert "Node" not in result.output and "npx" not in result.output
