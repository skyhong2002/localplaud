"""Regression coverage for modal state overriding author display rules."""

from __future__ import annotations


def test_hidden_rule_wins_over_modal_display(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'ui.db'}")
    monkeypatch.setenv(
        "LOCALPLAUD_PLAUD__OFFICIAL__TOKENS_PATH", str(tmp_path / "tokens.json")
    )
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    page = TestClient(app).get("/")
    assert page.status_code == 200
    assert "[hidden] { display:none !important; }" in page.text
    assert 'class="import-backdrop" id="import-backdrop" hidden' in page.text
