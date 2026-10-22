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


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_outline: run the real LLM outline generator (no autouse stub)"
    )


@pytest.fixture(autouse=True)
def _stub_outline_llm(request, monkeypatch):
    """Keep unrelated pipeline tests off a real LLM for the outline stage.

    The stub is clearly labelled (provider ``test-outline-stub``) and still
    exercises the durable outline stage, persistence and provenance. Tests
    marked ``real_outline`` drive the real generator with their own fakes.
    """
    if request.node.get_closest_marker("real_outline"):
        return
    from localplaud.worker import outline

    def fake(transcript, settings, *, duration_ms=None):
        result = outline.time_slice_outline(transcript, duration_ms=duration_ms)
        return result | {
            "method": "llm",
            "provider": "test-outline-stub",
            "model": "stub",
            "prompt_version": outline.PROMPT_VERSION,
        }

    monkeypatch.setattr(outline, "generate_outline", fake)
