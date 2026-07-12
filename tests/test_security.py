"""Tests for the security hardening: API auth gate, path-id validation."""

from __future__ import annotations

import pytest


def _reset_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'s.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)


def test_api_auth_gate(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LOCALPLAUD_API__AUTH_TOKEN", "s3cret")
    _reset_db(monkeypatch, tmp_path)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    client = TestClient(app)
    # health check is always open (for load balancers / smoke tests)
    assert client.get("/healthz").status_code == 200
    # protected routes require the token
    assert client.get("/api/files").status_code == 401
    assert client.get("/api/files", headers={"X-Auth-Token": "wrong"}).status_code == 401
    assert client.get("/api/files", headers={"X-Auth-Token": "s3cret"}).status_code == 200
    assert client.get("/api/files?token=s3cret").status_code == 200


def test_api_open_without_token(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.delenv("LOCALPLAUD_API__AUTH_TOKEN", raising=False)
    _reset_db(monkeypatch, tmp_path)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    client = TestClient(app)
    assert client.get("/api/files").status_code == 200  # no token configured -> open


def test_web_login_session_and_logout(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.delenv("LOCALPLAUD_API__AUTH_TOKEN", raising=False)
    monkeypatch.setenv("LOCALPLAUD_API__LOGIN_PASSWORD", "correct horse")
    monkeypatch.setenv("LOCALPLAUD_API__SESSION_SECRET", "a-long-random-session-secret")
    _reset_db(monkeypatch, tmp_path)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    client = TestClient(app, base_url="https://testserver")
    redirected = client.get(
        "/settings?section=account", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert redirected.status_code == 303
    assert redirected.headers["location"].startswith("/login?next=")
    assert client.get("/api/files").status_code == 401

    wrong = client.post(
        "/login", data={"password": "wrong", "next": "/settings"}, follow_redirects=False
    )
    assert wrong.status_code == 401
    assert "localplaud_session=" not in wrong.headers.get("set-cookie", "")

    logged_in = client.post(
        "/login",
        data={"password": "correct horse", "next": "/settings"},
        follow_redirects=False,
    )
    assert logged_in.status_code == 303
    assert logged_in.headers["location"] == "/settings"
    cookie = logged_in.headers["set-cookie"]
    assert "localplaud_session=" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert client.get("/settings").status_code == 200

    logged_out = client.post("/logout", follow_redirects=False)
    assert logged_out.status_code == 303
    assert client.get(
        "/settings", headers={"Accept": "text/html"}, follow_redirects=False
    ).status_code == 303


def test_login_rejects_open_redirect_and_tampered_cookie(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.delenv("LOCALPLAUD_API__AUTH_TOKEN", raising=False)
    monkeypatch.setenv("LOCALPLAUD_API__LOGIN_PASSWORD", "pw")
    monkeypatch.setenv("LOCALPLAUD_API__SESSION_SECRET", "a-long-random-session-secret")
    _reset_db(monkeypatch, tmp_path)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    client = TestClient(app, base_url="https://testserver")
    response = client.post(
        "/login", data={"password": "pw", "next": "https://evil.example"}, follow_redirects=False
    )
    assert response.headers["location"] == "/"
    client.cookies.set("localplaud_session", "tampered.value")
    assert client.get("/", headers={"Accept": "text/html"}, follow_redirects=False).status_code == 303


def test_bearer_token_remains_supported(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LOCALPLAUD_API__AUTH_TOKEN", "api-secret")
    monkeypatch.setenv("LOCALPLAUD_API__LOGIN_PASSWORD", "web-secret")
    monkeypatch.setenv("LOCALPLAUD_API__SESSION_SECRET", "a-long-random-session-secret")
    _reset_db(monkeypatch, tmp_path)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    client = TestClient(app)
    assert client.get("/api/files", headers={"Authorization": "Bearer api-secret"}).status_code == 200


def test_file_id_path_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path))
    from localplaud.config import get_settings
    from localplaud.store.files import file_dir

    get_settings(reload=True)
    assert file_dir("abc123DEF-_").exists()
    for bad in ("../etc", "a/b", "..", "x" * 200, ""):
        with pytest.raises(ValueError):
            file_dir(bad)


def test_ollama_embedder_dispatch():
    from localplaud.config import EmbeddingsConfig
    from localplaud.embeddings.base import build_embedder
    from localplaud.embeddings.ollama_embed import OllamaEmbedder

    e = build_embedder(EmbeddingsConfig(provider="ollama"))
    assert isinstance(e, OllamaEmbedder)
    assert e.name == "ollama:bge-m3"
