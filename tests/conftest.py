"""Global test isolation from the operator's deployment files and secrets.

A checkout that is also a deployment has a private ``config.toml`` and ``.env`` in
the repository root. Tests must see defaults plus what they set themselves, never
production providers, URLs or credentials — and ``get_settings()`` would otherwise
export ``.env`` into ``os.environ`` for every later test.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

import localplaud.config as _config

os.environ["LOCALPLAUD_CONFIG"] = str(Path(tempfile.mkdtemp()) / "absent-config.toml")
_config.Settings.model_config["env_file"] = None
_config.load_dotenv = lambda *args, **kwargs: False


@pytest.fixture(autouse=True)
def _isolate_web_login_environment(monkeypatch):
    monkeypatch.setenv("LOCALPLAUD_API__AUTH_TOKEN", "")
    monkeypatch.setenv("LOCALPLAUD_API__ACCOUNTS_ENABLED", "false")
    monkeypatch.setenv("LOCALPLAUD_API__GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setenv("LOCALPLAUD_API__GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("LOCALPLAUD_API__LOGIN_PASSWORD", "")
    monkeypatch.setenv("LOCALPLAUD_API__SESSION_SECRET", "")


@pytest.fixture
def force_journal_mode():
    """Rebind an engine's pooled connections to a given SQLite journal mode.

    Connections configure the journal mode from settings on connect, so a test
    that wants the non-default mode has to change the setting and drop the
    pooled connections holding the old one — SQLite refuses to leave WAL while
    another connection is open. Returns the mode actually in force.
    """
    from sqlalchemy import text

    from localplaud.config import get_settings

    def apply(engine, journal_mode: str) -> str:
        get_settings().store.sqlite_journal_mode = journal_mode
        engine.dispose()
        with engine.connect() as connection:
            mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        return str(mode).lower()

    return apply
