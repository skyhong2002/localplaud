"""Authorization acceptance: no legacy key, pending-data boundary, OIDC, and recovery."""

import asyncio
import re
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from localplaud.api import accounts as auth
from localplaud.config import ApiConfig, get_settings
from localplaud.db.models import AccountUser, AuthTransaction, Base, BrowserSession, OAuthIdentity
from localplaud.db.session import session_scope

PASSWORD = "a-long-test-password"


@pytest.fixture
def client(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.api.app import app

    config = tmp_path / "empty.toml"
    config.write_text("")
    monkeypatch.setenv("LOCALPLAUD_CONFIG", str(config))
    for key, value in {
        "STORE__DATABASE_URL": f"sqlite:///{tmp_path / 'auth.db'}",
        "API__ACCOUNTS_ENABLED": "true",
        "API__SESSION_SECRET": "test-random-secret",
        "API__OWNER_EMAIL": "owner@example.com",
        "API__OWNER_USERNAME": "sky",
        "API__PUBLIC_URL": "https://testserver",
        "API__AUTH_TOKEN": "old-secret",
        "API__LOGIN_PASSWORD": "old-password",
        "API__GOOGLE_CLIENT_ID": "test-client",
        "API__GOOGLE_CLIENT_SECRET": "test-secret",
        "API__GOOGLE_REDIRECT_URI": "https://testserver/auth/google/callback",
    }.items():
        monkeypatch.setenv("LOCALPLAUD_" + key, value)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    Base.metadata.create_all(db_session.get_engine())
    monkeypatch.setattr(
        auth, "render", lambda request, name, **context: JSONResponse({"template": name, **context})
    )
    result = TestClient(
        app,
        base_url="https://testserver",
        headers={"Origin": "https://testserver"},
        follow_redirects=False,
    )
    yield result
    result.close()
    db_session.get_engine().dispose()


def create_user(username="someone", role="viewer", status="active", password=True):
    with session_scope() as db:
        user = AccountUser(
            username=username,
            email=f"{username}@example.com",
            role=role,
            status=status,
            owner_slot="owner" if role == "owner" else None,
            password_hash=auth.PASSWORDS.hash(PASSWORD) if password else None,
        )
        db.add(user)
        db.flush()
        return user.id


def login(client, username="someone"):
    result = client.post("/login", data={"identifier": username, "password": PASSWORD})
    assert result.status_code == 303
    return result


def test_registration_reserves_owner_and_creates_pending(client):
    for username, email in [("sky", "intruder@example.com"), ("intruder", "owner@example.com")]:
        result = client.post(
            "/register",
            data={
                "username": username,
                "email": email,
                "password": PASSWORD,
                "password_confirm": PASSWORD,
            },
        )
        assert result.json()["error"]
    result = client.post(
        "/register",
        data={
            "username": "new-user",
            "email": "new@example.com",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
        },
    )
    assert result.status_code == 303 and result.headers["location"] == "/account"
    account = client.get("/account").json()
    assert account["account_user"]["status"] == "pending"
    assert account["sessions"][0]["current"]
    with session_scope() as db:
        assert db.scalar(select(AccountUser)).password_hash.startswith("$argon2id$")


def test_pending_cannot_reach_any_private_route(client):
    from localplaud.api.app import app

    create_user(status="pending")
    login(client)
    for route in app.routes:
        path = getattr(route, "path", "")
        if path in auth.PUBLIC or path.startswith(
            ("/static", "/share/", "/api/worker/v1", "/account", "/logout")
        ):
            continue
        path = re.sub(r"\{[^}]+\}", "123", path)
        for method in getattr(route, "methods", []):
            if method == "OPTIONS":
                continue
            response = client.request(method, path)
            assert response.status_code == 403, (method, path, response.status_code)
    response = client.get("/", headers={"HX-Request": "true"})
    assert response.headers["HX-Redirect"] == "/account"


def test_legacy_tokens_and_unbound_sessions_rejected(client):
    for headers, query in [
        ({"X-Auth-Token": "old-secret"}, ""),
        ({"Authorization": "Bearer old-secret"}, ""),
        ({}, "?token=old-secret"),
    ]:
        assert client.get("/api/files" + query, headers=headers).status_code == 401
    with session_scope() as db:
        db.add(
            BrowserSession(
                token_hash=auth.digest("legacy"), expires_at=auth.now() + timedelta(days=1)
            )
        )
    client.cookies.set(auth.COOKIE, "legacy")
    assert client.get("/api/files").status_code == 401
    response = client.post("/login", data={"password": "old-password"})
    assert response.json()["error"]


def test_viewer_read_allowlist_and_mutation_denial(client):
    create_user()
    login(client)
    assert client.get("/api/files").status_code == 200
    for path in [
        "/settings",
        "/admin/users",
        "/api/plaud/auth/status",
        "/status",
        "/api/preferences/workspace",
        "/api/new-secret-route",
    ]:
        assert client.get(path).status_code == 403
    for path in ["/ask", "/file/x/ask", "/api/folders", "/api/files/x/refresh-cloud-artifacts"]:
        assert client.post(path).status_code == 403
    assert client.get("/file/missing/export.md").status_code == 404
    assert client.get("/file/missing/export/notes.txt").status_code == 404
    assert client.get("/api/notes/123/export.md").status_code == 404
    assert client.get("/api/notes/123/history").status_code == 404
    assert (
        client.post(
            "/api/files/export", json={"file_ids": ["missing"], "transcript_format": "txt"}
        ).status_code
        == 404
    )


def test_admin_permissions_owner_protection_and_immediate_revocation(client):
    owner = create_user("sky", role="owner")
    admin = create_user("admin", role="admin")
    viewer = create_user("someone", status="pending")
    login(client, "admin")
    assert (
        client.post(f"/admin/users/{viewer}", data={"role": "viewer", "status": "active"})
        .headers["location"]
        .endswith("notice=updated")
    )
    assert (
        "error"
        in client.post(
            f"/admin/users/{viewer}", data={"role": "admin", "status": "active"}
        ).headers["location"]
    )
    assert (
        "error"
        in client.post(
            f"/admin/users/{owner}", data={"role": "viewer", "status": "disabled"}
        ).headers["location"]
    )
    login(client, "someone")
    cookie = client.cookies.get(auth.COOKIE)
    login(client, "sky")
    assert (
        "notice"
        in client.post(
            f"/admin/users/{viewer}", data={"role": "viewer", "status": "disabled"}
        ).headers["location"]
    )
    assert (
        "notice"
        in client.post(
            f"/admin/users/{admin}", data={"role": "viewer", "status": "active"}
        ).headers["location"]
    )
    client.cookies.clear()
    client.cookies.set(auth.COOKIE, cookie)
    assert client.get("/account").status_code == 401


def test_sessions_are_private_password_change_and_operator_recovery(client):
    one = create_user("sky", role="owner")
    two = create_user("admin", role="admin")
    login(client, "admin")
    session_id = client.get("/account").json()["sessions"][0]["id"]
    client.cookies.clear()
    login(client, "sky")
    assert client.post(f"/account/sessions/{session_id}/revoke").status_code == 404
    assert client.post(f"/api/sessions/{session_id}/revoke").status_code == 404
    assert all(s["id"] != session_id for s in client.get("/account").json()["sessions"])
    response = client.post(
        "/account/password",
        data={
            "current_password": PASSWORD,
            "password": PASSWORD + "2",
            "password_confirm": PASSWORD + "2",
        },
    )
    assert response.headers["location"] == "/account?notice=password"
    auth.recover_owner_password(PASSWORD)
    assert client.get("/account").status_code == 401
    login(client, "sky")
    with session_scope() as db:
        assert db.get(AccountUser, one).role == "owner"
        assert db.get(AccountUser, two).role == "admin"


def test_csrf_and_safe_redirect_and_cache(client):
    create_user()
    for origin in ["https://evil.example", "null", "https://testserver.evil.example"]:
        assert (
            client.post(
                "/login",
                headers={"Origin": origin},
                data={"identifier": "someone", "password": PASSWORD},
            ).status_code
            == 403
        )
    assert client.post("/register", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    response = client.post(
        "/login", data={"identifier": "someone", "password": PASSWORD, "next": "//evil.example"}
    )
    assert response.headers["location"] == "/"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "Cookie" in response.headers["Vary"]


def oauth_start(client):
    start = client.get("/auth/google")
    assert start.status_code == 303
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid email profile"]
    return query


def test_google_owner_bootstrap_replay_and_persistent_owner(client, monkeypatch):
    async def exchange(code, transaction):
        return {
            "sub": "google-owner",
            "email": "owner@example.com",
            "email_verified": True,
            "hd": "example.com",
        }

    monkeypatch.setattr(auth, "exchange_google", exchange)
    query = oauth_start(client)
    callback = "/auth/google/callback?" + urlencode_test(state=query["state"][0], code="code")
    response = client.get(callback)
    assert response.headers["location"] == "/"
    account = client.get("/account").json()["account_user"]
    assert account["username"] == "sky" and account["role"] == "owner"
    assert "error" in client.get(callback).headers["location"]
    get_settings().api.owner_email = "changed@example.com"
    query = oauth_start(client)
    client.get("/auth/google/callback?" + urlencode_test(state=query["state"][0], code="code"))
    assert client.get("/account").json()["account_user"]["id"] == account["id"]


def urlencode_test(**kwargs):
    from urllib.parse import urlencode

    return urlencode(kwargs)


def test_google_binding_state_expiry_and_email_collision(client, monkeypatch):
    create_user()
    called = []

    async def exchange(code, transaction):
        called.append(code)
        return {
            "sub": "someone-sub",
            "email": "someone@example.com",
            "email_verified": True,
            "hd": "example.com",
        }

    monkeypatch.setattr(auth, "exchange_google", exchange)
    query = oauth_start(client)
    assert "error" in client.get("/auth/google/callback?state=wrong&code=x").headers["location"]
    assert not called
    query = oauth_start(client)
    result = client.get(
        "/auth/google/callback?" + urlencode_test(state=query["state"][0], code="x")
    )
    assert result.headers["location"] == "/login?error=link_required"
    with session_scope() as db:
        assert db.scalar(select(OAuthIdentity)) is None
    login(client)
    query = parse_qs(urlsplit(client.get("/auth/google?link=1").headers["location"]).query)
    result = client.get(
        "/auth/google/callback?" + urlencode_test(state=query["state"][0], code="x")
    )
    assert result.headers["location"] == "/account?notice=linked"
    assert client.get("/account").json()["google_linked"]
    query = oauth_start(client)
    with session_scope() as db:
        tx = db.scalar(
            select(AuthTransaction).where(
                AuthTransaction.state_hash == auth.digest(query["state"][0])
            )
        )
        tx.expires_at = auth.now() - timedelta(seconds=1)
    assert (
        "error"
        in client.get(
            "/auth/google/callback?" + urlencode_test(state=query["state"][0], code="x")
        ).headers["location"]
    )


def test_oidc_signature_nonce_audience_issuer_email_verification(client, monkeypatch):
    from authlib.jose import JsonWebKey, jwt
    from authlib.jose.errors import JoseError
    from joserfc.errors import JoseError as RFCJoseError

    key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "one"})
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "test-client",
        "sub": "subject",
        "iat": int(auth.now().timestamp()),
        "exp": int(auth.now().timestamp()) + 300,
        "nonce": "correct-nonce",
        "email": "owner@example.com",
        "email_verified": True,
    }
    payload = {}

    class FakeOAuth:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def fetch_token(self, *a, **kw):
            return payload

    class FakeHTTP(FakeOAuth):
        async def get(self, *a, **kw):
            return SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: {"keys": [key.as_dict()]}
            )

    monkeypatch.setattr(auth, "AsyncOAuth2Client", FakeOAuth)
    monkeypatch.setattr(auth.httpx, "AsyncClient", FakeHTTP)
    tx = SimpleNamespace(verifier="verifier", nonce="correct-nonce")

    def run(data, signing_key=key):
        payload["id_token"] = jwt.encode({"alg": "RS256", "kid": "one"}, data, signing_key).decode()
        return asyncio.run(auth.exchange_google("code", tx))

    assert run(claims)["sub"] == "subject"
    for patch in (
        {"nonce": "wrong"},
        {"aud": "evil"},
        {"iss": "https://evil.example"},
        {"email_verified": False},
        {"email_verified": "true"},
        {"exp": 1},
    ):
        with pytest.raises((JoseError, RFCJoseError, ValueError)):
            run(claims | patch)
    with pytest.raises((JoseError, RFCJoseError, ValueError)):
        run(claims, JsonWebKey.generate_key("RSA", 2048, is_private=True))


def test_migration_is_additive_idempotent_and_fail_closed(tmp_path):
    from localplaud.db.migrations import migrate_account_sessions

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE browser_sessions (id INTEGER PRIMARY KEY, token_hash VARCHAR(64))")
        )
        conn.execute(text("INSERT INTO browser_sessions VALUES (1, 'legacy')"))
    Base.metadata.create_all(engine)
    migrate_account_sessions(engine)
    migrate_account_sessions(engine)
    assert "user_id" in {c["name"] for c in inspect(engine).get_columns("browser_sessions")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT token_hash, user_id FROM browser_sessions")).one() == (
            "legacy",
            None,
        )
    with pytest.raises(ValueError):
        ApiConfig(accounts_enabled=True)


def test_rate_limit_ignores_forwarded_headers(client):
    for index in range(31):
        response = client.post(
            "/login",
            headers={"X-Forwarded-For": f"198.51.100.{index}"},
            data={"identifier": "none", "password": "x"},
        )
    assert response.status_code == 429


def test_google_password_recovery_needs_recent_verified_auth(client, monkeypatch):
    user_id = create_user("sky", role="owner")
    with session_scope() as db:
        db.add(OAuthIdentity(user_id=user_id, subject="owner-sub"))

    async def exchange(code, transaction):
        return {
            "sub": "owner-sub",
            "email": "sky@example.com",
            "email_verified": True,
            "hd": "example.com",
        }

    monkeypatch.setattr(auth, "exchange_google", exchange)
    query = oauth_start(client)
    client.get("/auth/google/callback?" + urlencode_test(state=query["state"][0], code="x"))
    with session_scope() as db:
        original_time = db.scalar(select(BrowserSession)).authenticated_at
    response = client.post(
        "/account/password", data={"password": PASSWORD + "2", "password_confirm": PASSWORD + "2"}
    )
    assert response.headers["location"] == "/account?notice=password"
    with session_scope() as db:
        session = db.scalar(select(BrowserSession))
        assert session.authenticated_at == original_time
        session.authenticated_at = auth.now() - timedelta(minutes=11)
    response = client.post(
        "/account/password", data={"password": PASSWORD, "password_confirm": PASSWORD}
    )
    assert response.headers["location"] == "/account?error=password"


def test_link_transaction_cannot_cross_sessions(client, monkeypatch):
    create_user()
    login(client)
    query = parse_qs(urlsplit(client.get("/auth/google?link=1").headers["location"]).query)

    async def exchange(code, transaction):
        return {
            "sub": "subject",
            "email": "someone@example.com",
            "email_verified": True,
            "hd": "example.com",
        }

    monkeypatch.setattr(auth, "exchange_google", exchange)
    client.cookies.delete(auth.COOKIE)
    response = client.get(
        "/auth/google/callback?" + urlencode_test(state=query["state"][0], code="x")
    )
    assert "error" in response.headers["location"]
    with session_scope() as db:
        assert db.scalar(select(OAuthIdentity)) is None


def test_concurrent_callback_consumption_is_single_use(client):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import delete

    query = oauth_start(client)
    state_hash = auth.digest(query["state"][0])

    def consume():
        with session_scope() as db:
            return db.scalar(
                delete(AuthTransaction)
                .where(AuthTransaction.state_hash == state_hash)
                .returning(AuthTransaction.state_hash)
            )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: consume(), range(4)))
    assert results.count(state_hash) == 1


def test_incomplete_google_keeps_local_login_and_operator_account_recovery(client):
    create_user(status="pending")
    get_settings().api.google_client_secret = None
    assert not client.get("/login").json()["google_enabled"]
    assert client.get("/auth/google").headers["location"] == "/login?error=google_unavailable"
    auth.recover_account_password(PASSWORD + "recovered", username="someone")
    result = client.post(
        "/login", data={"identifier": "someone@example.com", "password": PASSWORD + "recovered"}
    )
    assert result.headers["location"] == "/account"
    with pytest.raises(ValueError):
        auth.recover_owner_password(PASSWORD)


@pytest.mark.parametrize("claim_patch", [{}, {"hd": ""}, {"hd": "wrong.example"}])
def test_unhosted_google_email_cannot_bootstrap_owner(client, monkeypatch, claim_patch):
    async def exchange(code, transaction):
        return {
            "sub": "candidate-owner",
            "email": "owner@example.com",
            "email_verified": True,
            **claim_patch,
        }

    monkeypatch.setattr(auth, "exchange_google", exchange)
    query = oauth_start(client)
    response = client.get(
        "/auth/google/callback?" + urlencode_test(state=query["state"][0], code="x")
    )
    assert response.headers["location"] == "/login?error=google"
    with session_scope() as db:
        assert db.scalar(select(AccountUser)) is None
        assert db.scalar(select(OAuthIdentity)) is None


def test_gmail_bootstrap_and_unhosted_new_user_pending(client, monkeypatch):
    get_settings().api.owner_email = "owner@gmail.com"
    claims = {"sub": "gmail-owner", "email": "owner@gmail.com", "email_verified": True}

    async def exchange(code, transaction):
        return claims

    monkeypatch.setattr(auth, "exchange_google", exchange)
    query = oauth_start(client)
    response = client.get(
        "/auth/google/callback?" + urlencode_test(state=query["state"][0], code="x")
    )
    assert response.headers["location"] == "/"
    assert client.get("/account").json()["account_user"]["role"] == "owner"
    claims = {"sub": "third-party-user", "email": "other@example.com", "email_verified": True}
    query = oauth_start(client)
    client.get("/auth/google/callback?" + urlencode_test(state=query["state"][0], code="x"))
    assert client.get("/account").json()["account_user"]["status"] == "pending"


def test_unhosted_google_email_cannot_link_local_account(client, monkeypatch):
    create_user()
    login(client)

    async def exchange(code, transaction):
        return {"sub": "third-party-sub", "email": "someone@example.com", "email_verified": True}

    monkeypatch.setattr(auth, "exchange_google", exchange)
    query = parse_qs(urlsplit(client.get("/auth/google?link=1").headers["location"]).query)
    response = client.get(
        "/auth/google/callback?" + urlencode_test(state=query["state"][0], code="x")
    )
    assert response.headers["location"] == "/login?error=google"
    with session_scope() as db:
        assert db.scalar(select(OAuthIdentity)) is None


@pytest.mark.parametrize(
    "mutation", ["revoke", "password_reset", "role_change", "disable", "expire", "stale_auth"]
)
def test_link_rechecks_auth_after_awaiting_google(client, monkeypatch, mutation):
    from sqlalchemy import delete

    user_id = create_user(role="admin")
    login(client)
    query = parse_qs(urlsplit(client.get("/auth/google?link=1").headers["location"]).query)

    async def exchange(code, transaction):
        # Middleware has already authenticated the callback. The remote response
        # boundary lets another request revoke/alter its authorizing session.
        with session_scope() as db:
            row = db.get(BrowserSession, transaction.session_id)
            user = db.get(AccountUser, user_id)
            if mutation == "disable":
                user.status = "disabled"
            elif mutation == "expire":
                row.expires_at = auth.now() - timedelta(seconds=1)
            elif mutation == "stale_auth":
                row.authenticated_at = auth.now() - timedelta(minutes=11)
            else:
                if mutation == "role_change":
                    user.role = "viewer"
                if mutation == "password_reset":
                    user.password_hash = auth.PASSWORDS.hash("replacement-password")
                db.execute(delete(BrowserSession).where(BrowserSession.user_id == user_id))
        return {
            "sub": "linked-sub",
            "email": "someone@example.com",
            "email_verified": True,
            "hd": "example.com",
        }

    monkeypatch.setattr(auth, "exchange_google", exchange)
    response = client.get(
        "/auth/google/callback?" + urlencode_test(state=query["state"][0], code="x")
    )
    assert response.headers["location"] == "/login?error=google"
    assert auth.COOKIE + "=" not in response.headers.get("set-cookie", "")
    with session_scope() as db:
        assert db.scalar(select(OAuthIdentity)) is None


def test_link_cookie_rotation_cannot_reuse_recycled_session_id(client, monkeypatch):
    create_user()
    login(client)
    session_id = client.get("/account").json()["sessions"][0]["id"]
    query = parse_qs(urlsplit(client.get("/auth/google?link=1").headers["location"]).query)
    login(client)
    # SQLite may recycle integer ids after rotation; the exact cookie remains distinct.
    assert client.get("/account").json()["sessions"][0]["id"] == session_id

    async def exchange(code, transaction):
        raise AssertionError("A replaced session must not reach token exchange")

    monkeypatch.setattr(auth, "exchange_google", exchange)
    response = client.get(
        "/auth/google/callback?" + urlencode_test(state=query["state"][0], code="x")
    )
    assert response.headers["location"] == "/login?error=google"


def test_link_publication_serializes_with_concurrent_revocation(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import delete

    user_id = create_user()
    login(client)
    query = parse_qs(urlsplit(client.get("/auth/google?link=1").headers["location"]).query)

    async def exchange(code, transaction):
        return {
            "sub": "linked-sub",
            "email": "someone@example.com",
            "email_verified": True,
            "hd": "example.com",
        }

    monkeypatch.setattr(auth, "exchange_google", exchange)
    original_new_session = auth.new_session
    started = Event()
    tasks = []
    with ThreadPoolExecutor(max_workers=1) as executor:

        def revoke_all():
            with session_scope() as db:
                started.set()
                db.execute(delete(BrowserSession).where(BrowserSession.user_id == user_id))

        def publishing(db, user, request, **kwargs):
            # The callback already read the authorizing session. A competing
            # revocation now queues behind its write lock and must remove even
            # the replacement session as soon as this transaction commits.
            tasks.append(executor.submit(revoke_all))
            assert started.wait(timeout=2)
            assert not tasks[0].done()
            return original_new_session(db, user, request, **kwargs)

        monkeypatch.setattr(auth, "new_session", publishing)
        response = client.get(
            "/auth/google/callback?" + urlencode_test(state=query["state"][0], code="x")
        )
        assert response.headers["location"] == "/account?notice=linked"
        tasks[0].result(timeout=5)
    assert client.get("/account").status_code == 401
    with session_scope() as db:
        assert db.scalar(select(BrowserSession).where(BrowserSession.user_id == user_id)) is None
