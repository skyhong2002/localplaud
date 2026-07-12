"""Global test isolation from operator-only Web login secrets in .env."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_web_login_environment(monkeypatch):
    monkeypatch.setenv("LOCALPLAUD_API__LOGIN_PASSWORD", "")
    monkeypatch.setenv("LOCALPLAUD_API__SESSION_SECRET", "")
