"""Global test isolation from operator-only Web login secrets in .env."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_web_login_environment(monkeypatch):
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
