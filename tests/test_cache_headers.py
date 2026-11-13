"""One cache policy in every auth mode: only public shell assets may be cached.

Versioned ``/static`` assets and the versioned translation catalog are immutable
in the browser cache; everything else (pages, API, ``/sw.js``, rejections) is
``private, no-store`` whether the instance uses a token, a password or accounts.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from localplaud.api.static_assets import static_url
from localplaud.i18n import catalog_script

YEAR = "max-age=31536000, immutable"
CATALOG = "/i18n/zh-Hant-TW.js"


def _configure(monkeypatch, tmp_path, env: dict[str, str]) -> None:
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    config = tmp_path / "empty.toml"
    config.write_text("")
    monkeypatch.setenv("LOCALPLAUD_CONFIG", str(config))
    for key in ("AUTH_TOKEN", "LOGIN_PASSWORD", "SESSION_SECRET", "ACCOUNTS_ENABLED"):
        monkeypatch.delenv(f"LOCALPLAUD_API__{key}", raising=False)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'cache.db'}")
    for key, value in env.items():
        monkeypatch.setenv(f"LOCALPLAUD_{key}", value)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.db.session import init_db

    init_db()


def _token_client(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, {"API__AUTH_TOKEN": "cache-test-token"})
    from localplaud.api.app import app

    anonymous = TestClient(app, base_url="https://testserver", follow_redirects=False)
    signed_in = TestClient(
        app,
        base_url="https://testserver",
        follow_redirects=False,
        headers={"X-Auth-Token": "cache-test-token"},
    )
    return anonymous, signed_in


def _password_client(monkeypatch, tmp_path):
    _configure(
        monkeypatch,
        tmp_path,
        {
            "API__LOGIN_PASSWORD": "correct horse battery",
            "API__SESSION_SECRET": "a-long-random-session-secret",
        },
    )
    from localplaud.api.app import app

    anonymous = TestClient(app, base_url="https://testserver", follow_redirects=False)
    signed_in = TestClient(app, base_url="https://testserver", follow_redirects=False)
    login = signed_in.post("/login", data={"password": "correct horse battery", "next": "/"})
    assert login.status_code == 303
    return anonymous, signed_in


def _accounts_client(monkeypatch, tmp_path):
    _configure(
        monkeypatch,
        tmp_path,
        {
            "API__ACCOUNTS_ENABLED": "true",
            "API__SESSION_SECRET": "test-random-secret",
            "API__OWNER_EMAIL": "owner@example.com",
            "API__OWNER_USERNAME": "owner",
            "API__PUBLIC_URL": "https://testserver",
        },
    )
    from localplaud.api import accounts as auth
    from localplaud.api.app import app
    from localplaud.db.models import AccountUser
    from localplaud.db.session import session_scope

    with session_scope() as db:
        db.add(
            AccountUser(
                username="owner",
                email="owner@example.com",
                role="owner",
                status="active",
                owner_slot="owner",
                password_hash=auth.PASSWORDS.hash("a-long-test-password"),
            )
        )
    origin = {"Origin": "https://testserver"}
    anonymous = TestClient(
        app, base_url="https://testserver", follow_redirects=False, headers=origin
    )
    signed_in = TestClient(
        app, base_url="https://testserver", follow_redirects=False, headers=origin
    )
    login = signed_in.post(
        "/login", data={"identifier": "owner", "password": "a-long-test-password"}
    )
    assert login.status_code == 303
    return anonymous, signed_in


MODES = {
    "token": _token_client,
    "password": _password_client,
    "accounts": _accounts_client,
}


@pytest.fixture(params=sorted(MODES))
def clients(request, monkeypatch, tmp_path):
    anonymous, signed_in = MODES[request.param](monkeypatch, tmp_path)
    yield request.param, anonymous, signed_in
    anonymous.close()
    signed_in.close()
    import localplaud.db.session as db_session

    db_session.get_engine().dispose()


def test_versioned_static_assets_are_immutable_in_every_mode(clients):
    _, anonymous, signed_in = clients
    url = static_url("css/app.css")
    assert re.fullmatch(r"/static/css/app\.css\?v=[0-9a-f]{12}", url)
    # Static files are public in every mode; signed in or not, the policy is the same.
    for client in (anonymous, signed_in):
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers["cache-control"] == f"public, {YEAR}"
        assert "Cookie" not in response.headers.get("vary", "")
        # A conditional revalidation keeps the same policy on its 304.
        revalidated = client.get(url, headers={"If-None-Match": response.headers["etag"]})
        assert revalidated.status_code == 304
        assert revalidated.headers["cache-control"] == f"public, {YEAR}"


@pytest.mark.parametrize(
    "url",
    ["/static/css/app.css", "/static/css/app.css?v=000000000000", "/static/htmx-1.9.12.min.js"],
)
def test_unversioned_or_stale_static_assets_revalidate(clients, url):
    _, _, signed_in = clients
    response = signed_in.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"]


def test_missing_static_asset_is_not_cacheable(clients):
    _, _, signed_in = clients
    response = signed_in.get(static_url("css/does-not-exist.css"))
    assert response.status_code == 404
    assert response.headers["cache-control"] == "private, no-store"


def test_versioned_catalog_is_privately_immutable_and_stale_revalidates(clients):
    _, anonymous, signed_in = clients
    script = catalog_script("zh-Hant-TW")
    assert script is not None
    version = script[1]

    current = signed_in.get(f"{CATALOG}?v={version}")
    assert current.status_code == 200
    assert current.headers["cache-control"] == f"private, {YEAR}"
    assert current.headers["etag"] == f'"{version}"'
    assert "Cookie" not in current.headers.get("vary", "")

    stale = signed_in.get(f"{CATALOG}?v=000000000000")
    assert stale.status_code == 200
    assert stale.headers["cache-control"] == "private, no-cache"

    # The catalog stays behind the session gate; a rejection is never cached.
    rejected = anonymous.get(f"{CATALOG}?v={version}")
    assert rejected.status_code in {303, 401}
    assert rejected.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("path", ["/home", "/settings", "/api/files", "/sw.js", "/healthz"])
def test_pages_api_and_service_worker_are_never_stored(clients, path):
    mode, anonymous, signed_in = clients
    response = signed_in.get(path)
    assert response.status_code == 200, (mode, path, response.status_code)
    assert response.headers["cache-control"] == "private, no-store"
    if mode == "accounts":
        assert "Cookie" in response.headers["vary"]
    if path in {"/home", "/api/files"}:
        rejected = anonymous.get(path)
        assert rejected.status_code in {303, 401}
        assert rejected.headers["cache-control"] == "private, no-store"


def test_htmx_fragment_is_never_stored(clients):
    _, _, signed_in = clients
    response = signed_in.get("/home", headers={"HX-Request": "true", "HX-Target": "app-view"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"


def test_shell_references_the_hashes_the_server_treats_as_immutable(clients):
    _, _, signed_in = clients
    page = signed_in.get("/home").text
    urls = re.findall(r'(?:href|src)="(/static/[^"]+\?v=[^"]+)"', page)
    assert {u.split("?")[0] for u in urls} >= {
        "/static/css/app.css",
        "/static/js/app.js",
        "/static/htmx-1.9.12.min.js",
    }
    for url in urls:
        assert signed_in.get(url).headers["cache-control"] == f"public, {YEAR}", url
