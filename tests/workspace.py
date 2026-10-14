"""Workspace preference helpers shared by UI tests."""

from __future__ import annotations


def set_ask_enabled(client, enabled: bool = True) -> None:
    """Ask is off by default; tests that exercise it turn it on first."""
    preferences = client.get("/api/preferences/workspace").json()
    response = client.put("/api/preferences/workspace", json=preferences | {"ask_enabled": enabled})
    assert response.status_code == 200
    assert response.json()["ask_enabled"] is enabled
