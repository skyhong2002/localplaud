"""CLI smoke tests via typer's CliRunner — non-blocking commands only.

`serve`/`run`/`poll`/`work` block (server / infinite loop), so they are never
invoked here; we only assert they show up in --help.
"""

from __future__ import annotations

import os

from typer.testing import CliRunner

import localplaud.db.session as db_session
from localplaud.cli import app
from localplaud.config import get_settings

runner = CliRunner()


def _isolate(monkeypatch, tmp_path):
    """No ambient LOCALPLAUD_* env, no config.toml/.env from the repo, and a
    fresh settings singleton."""
    for key in list(os.environ):
        if key.startswith("LOCALPLAUD_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    get_settings(reload=True)


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for cmd in ("init", "auth", "poll", "work", "run", "ls", "ask", "serve"):
        assert cmd in result.output


def test_auth_help_lists_subcommands():
    result = runner.invoke(app, ["auth", "--help"])

    assert result.exit_code == 0
    assert "check" in result.output
    assert "import" in result.output


def test_init_creates_database(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    db_file = tmp_path / "data" / "cli-test.db"
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{db_file}")
    get_settings(reload=True)
    # Engine is a lazy module singleton; reset so it binds to the tmp database
    # (monkeypatch restores the previous engine/sessionmaker at teardown).
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert db_file.exists()
    assert "Database ready" in result.output


def test_auth_check_without_oauth_session_fails_helpfully(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    # Default provider is the official Open API; point the token cache at an
    # empty location so the developer's real ~/.plaud session can't leak in.
    monkeypatch.setenv(
        "LOCALPLAUD_PLAUD__OFFICIAL__TOKENS_PATH", str(tmp_path / "tokens.json")
    )
    get_settings(reload=True)

    result = runner.invoke(app, ["auth", "check"])

    assert result.exit_code != 0
    # The error explains what's missing and points at the fix.
    assert "auth login" in result.output


def test_auth_check_apse1_without_credentials_fails_helpfully(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)  # no token/cookie anywhere
    monkeypatch.setenv("LOCALPLAUD_PLAUD__PROVIDER", "apse1")
    get_settings(reload=True)

    result = runner.invoke(app, ["auth", "check"])

    assert result.exit_code != 0
    assert "credentials" in result.output
    assert "auth import" in result.output


def test_auth_import_parses_curl_file(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    curl = (
        "curl 'https://api-apse1.plaud.ai/user/me' \\\n"
        "  -H 'authorization: Bearer tok123' \\\n"
        "  -H 'x-device-id: dev-42' \\\n"
        "  -b 'sess=abc'"
    )
    curl_file = tmp_path / "req.curl"
    curl_file.write_text(curl)

    result = runner.invoke(app, ["auth", "import", "-f", str(curl_file)])

    assert result.exit_code == 0, result.output
    assert 'LOCALPLAUD_PLAUD__TOKEN="Bearer tok123"' in result.output
    assert 'LOCALPLAUD_PLAUD__API_BASE="https://api-apse1.plaud.ai"' in result.output
    assert 'LOCALPLAUD_PLAUD__COOKIE="sess=abc"' in result.output
    assert "x-device-id" in result.output  # extra headers preserved


def test_auth_import_rejects_curl_without_credentials(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    curl_file = tmp_path / "req.curl"
    curl_file.write_text("curl 'https://api-apse1.plaud.ai/user/me' -H 'accept: application/json'")

    result = runner.invoke(app, ["auth", "import", "-f", str(curl_file)])

    assert result.exit_code == 1
    assert "No Authorization or Cookie" in result.output


def test_auth_import_empty_input_fails(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    curl_file = tmp_path / "empty.curl"
    curl_file.write_text("   \n")

    result = runner.invoke(app, ["auth", "import", "-f", str(curl_file)])

    assert result.exit_code == 1
    assert "No input" in result.output
