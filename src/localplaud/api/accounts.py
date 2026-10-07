"""Account boundary, local passwords, and Google OIDC with server-side transactions."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.jose import JsonWebToken
from authlib.oidc.core import CodeIDToken
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..db.models import (
    AccountAuditEvent,
    AccountUser,
    AuthRateLimit,
    AuthTransaction,
    BrowserSession,
    OAuthIdentity,
)
from ..db.session import session_scope

router = APIRouter()
COOKIE = "localplaud_session"
OAUTH_COOKIE = "localplaud_oauth"
PASSWORDS = PasswordHasher()
_DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))
PUBLIC = {
    "/login",
    "/register",
    "/auth/google",
    "/auth/google/callback",
    "/healthz",
    "/favicon.ico",
    "/robots.txt",
}
# Explicit GET allowlist: new routes require a deliberate authorization decision.
VIEWER_PATHS = tuple(
    re.compile(p)
    for p in (
        r"/",
        r"/home",
        r"/search",
        r"/notes",
        r"/ui/sidebar-tags",
        r"/api/files",
        r"/api/files/picker",
        r"/api/organization",
        r"/file/[^/]+",
        r"/file/[^/]+/transcript-page",
        r"/audio/[^/]+(?:/waveform)?",
        r"/file/[^/]+/export\.md",
        r"/file/[^/]+/export/(?:audio|mind-map\.png|transcript\.(?:txt|srt|vtt|docx|pdf)|notes\.(?:md|txt|docx|pdf))",
        r"/api/files/[^/]+/note-assets/[^/]+",
        r"/api/files/[^/]+/outline",
        r"/api/notes",
        r"/api/notes/\d+/(?:history|history/\d+|export\.md)",
        r"/notes/\d+/versions/\d+",
        r"/file/[^/]+/notes/generated/[^/]+/versions/\d+",
        r"/api/files/[^/]+/summaries/\d+/history",
    )
)


def now():
    return datetime.now(UTC)


def aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def digest(value: str) -> str:
    return hmac.new(
        get_settings().api.session_secret.encode(), value.encode(), hashlib.sha256
    ).hexdigest()


def safe_next(value: str | None) -> str:
    if (
        not value
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(c) < 32 for c in value)
    ):
        return "/"
    return value[:2048]


def user_dict(user):
    return {key: getattr(user, key) for key in ("id", "username", "email", "role", "status")}


def google_enabled():
    cfg = get_settings().api
    uri = urlsplit(cfg.google_redirect_uri or "")
    return bool(
        cfg.google_client_id
        and cfg.google_client_secret
        and uri.netloc
        and uri.path == "/auth/google/callback"
        and uri.scheme == "https"
        and not uri.query
        and not uri.fragment
    )


def google_email_authoritative(claims):
    """Require Google-hosted email before using an email match to grant identity.

    A third-party Google Account can retain email_verified after mailbox ownership
    changes. See Google's verify-google-id-token guide. Existing subject-bound
    logins do not depend on email; this guard covers bootstrap and linking only.
    """
    email = claims.get("email", "")
    if claims.get("email_verified") is not True or not isinstance(email, str):
        return False
    domain = email.strip().lower().rpartition("@")[2]
    hosted_domain = claims.get("hd")
    return domain == "gmail.com" or (
        bool(domain) and isinstance(hosted_domain, str) and hosted_domain.strip().lower() == domain
    )


def render(request, name, **context):
    from .app import templates

    # Account pages have their own document shell, not the workspace's #app-view.
    # Older open tabs may still request these links as HTMX partial navigation.
    if (
        request.method == "GET"
        and request.headers.get("hx-request", "").lower() == "true"
        and request.headers.get("hx-target") == "app-view"
    ):
        destination = safe_next(
            request.url.path + ("?" + request.url.query if request.url.query else "")
        )
        return Response(headers={"HX-Redirect": destination})
    return templates.TemplateResponse(request=request, name=name, context=context)


def account_enabled():
    if not get_settings().api.accounts_enabled:
        raise HTTPException(404)


def current_user(request):
    user = getattr(request.state, "account_user", None)
    if not user:
        raise HTTPException(401)
    return user


def recent(request):
    session = getattr(request.state, "account_session", None)
    return bool(
        session
        and session.authenticated_at
        and now() - aware(session.authenticated_at) < timedelta(minutes=10)
    )


def csrf_ok(request):
    cfg = get_settings().api
    origin = urlsplit(cfg.public_url or str(request.base_url))
    trusted = (origin.scheme, origin.netloc)
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    supplied = request.headers.get("origin") or request.headers.get("referer")
    if not supplied:
        return False
    parsed = urlsplit(supplied)
    return (parsed.scheme, parsed.netloc) == trusted and not parsed.username


def authenticated(request):
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None, None
    with session_scope() as db:
        session = db.scalar(
            select(BrowserSession).where(
                BrowserSession.token_hash == digest(raw), BrowserSession.expires_at > now()
            )
        )
        user = db.get(AccountUser, session.user_id) if session and session.user_id else None
        if not user or user.status == "disabled":
            return None, None
        if now() - aware(session.last_seen_at) > timedelta(minutes=5):
            session.last_seen_at = now()
        return user, session


async def gate(request, call_next):
    path = request.url.path
    worker = path.startswith("/api/worker/v1/")
    if worker:
        return await call_next(request)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not csrf_ok(request):
        return JSONResponse({"detail": "請從本站重新提交。"}, status_code=403)
    user, session = authenticated(request)
    request.state.account_user = user_dict(user) if user else None
    request.state.account_session = session
    request.state.browser_session_id = session.id if session else None
    public = (
        path in PUBLIC
        or path.startswith("/static/")
        or (path.startswith("/share/") and request.method in {"GET", "HEAD"})
    )
    if not public:
        destination = None
        if not user:
            destination = "/login?" + urlencode(
                {"next": safe_next(path + ("?" + request.url.query if request.url.query else ""))}
            )
        else:
            own = path in {"/account", "/account/password", "/logout"} or bool(
                re.fullmatch(r"/account/sessions/\d+/revoke", path)
            )
            if user.status != "active" and not own:
                destination = "/account"
            elif (
                user.status == "active"
                and user.role == "viewer"
                and not own
                and not (
                    request.method in {"GET", "HEAD"}
                    and any(p.fullmatch(path) for p in VIEWER_PATHS)
                )
                and not (request.method == "POST" and path == "/api/files/export")
            ):
                if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                    response = render(
                        request,
                        "auth_denied.html",
                        account_user=user_dict(user),
                        error="此操作需要管理員權限。",
                    )
                    response.status_code = 403
                    return response
                return JSONResponse({"detail": "此操作需要管理員權限。"}, status_code=403)
        if destination:
            code = 401 if not user else 403
            if request.headers.get("hx-request", "").lower() == "true":
                return Response(status_code=code, headers={"HX-Redirect": destination})
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(destination, status_code=303)
            return JSONResponse(
                {"detail": "請先登入。" if not user else "帳號仍在等待管理員核准。"},
                status_code=code,
            )
    response = await call_next(request)
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Vary"] = ", ".join(filter(None, [response.headers.get("Vary"), "Cookie"]))
    return response


def rate_limit(request):
    """Atomic bounded SQLite counters; never inspect client-supplied forwarding headers."""
    with session_scope() as db:
        db.execute(delete(AuthRateLimit).where(AuthRateLimit.expires_at <= now()))
        for identity, maximum in (
            ("global", 240),
            (request.client.host if request.client else "unknown", 30),
        ):
            key = digest("rate:" + identity)
            stmt = insert(AuthRateLimit).values(
                key=key, count=1, expires_at=now() + timedelta(minutes=10)
            )
            count = db.scalar(
                stmt.on_conflict_do_update(
                    index_elements=[AuthRateLimit.key], set_={"count": AuthRateLimit.count + 1}
                ).returning(AuthRateLimit.count)
            )
            if count > maximum:
                # Commit the increment even when refusing; all workers see the same budget.
                db.commit()
                raise HTTPException(
                    429, "嘗試次數過多，請稍後再試。", headers={"Retry-After": "600"}
                )


def verify_password(password, hashed):
    try:
        return PASSWORDS.verify(hashed or _DUMMY_HASH, password) and bool(hashed)
    except (VerificationError, InvalidHashError):
        return False


def valid_password(password):
    return 12 <= len(password) <= 256


def new_session(db, user, request, *, auth_method="password", authenticated_at=None):
    cfg = get_settings().api
    token = secrets.token_urlsafe(32)
    db.execute(
        delete(BrowserSession)
        .where(BrowserSession.expires_at <= now())
        .execution_options(synchronize_session=False)
    )
    old = request.cookies.get(COOKIE)
    if old:
        db.execute(delete(BrowserSession).where(BrowserSession.token_hash == digest(old)))
    db.add(
        BrowserSession(
            user_id=user.id,
            auth_method=auth_method,
            authenticated_at=authenticated_at or now(),
            token_hash=digest(token),
            user_agent=request.headers.get("user-agent", "")[:256],
            expires_at=now() + timedelta(seconds=cfg.session_max_age_seconds),
        )
    )
    return token


def session_response(token, destination):
    cfg = get_settings().api
    response = RedirectResponse(destination, 303)
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=cfg.session_cookie_secure,
        samesite="lax",
        max_age=cfg.session_max_age_seconds,
        path="/",
    )
    return response


def login_page(request, next="/", error=None):
    return render(
        request,
        "auth_login.html",
        next=safe_next(next),
        error=(
            "此 Email 已有帳號，請先以密碼登入，再從帳號頁連結 Google。"
            if error == "link_required"
            else "登入未完成，請重試；若 Google 尚未設定，請使用密碼登入。"
        )
        if error
        else None,
        google_enabled=google_enabled(),
    )


def login_submit(request, identifier, password, next):
    rate_limit(request)
    if len(password) > 256 or len(identifier) > 254:
        return render(
            request,
            "auth_login.html",
            next=safe_next(next),
            error="帳號或密碼不正確。",
            google_enabled=google_enabled(),
        )
    with session_scope() as db:
        user = db.scalar(
            select(AccountUser).where(
                or_(
                    AccountUser.username == identifier.strip().lower(),
                    AccountUser.email == identifier.strip().lower(),
                )
            )
        )
        verified = verify_password(password, user.password_hash if user else None)
        if not verified or user.status == "disabled":
            return render(
                request,
                "auth_login.html",
                next=safe_next(next),
                error="帳號或密碼不正確。",
                google_enabled=google_enabled(),
            )
        token = new_session(db, user, request)
        destination = safe_next(next) if user.status == "active" else "/account"
    return session_response(token, destination)


@router.get("/register")
def register_page(request: Request):
    account_enabled()
    return render(request, "auth_register.html", error=None)


@router.post("/register")
def register_submit(
    request: Request,
    username: str = Form(),
    email: str = Form(),
    password: str = Form(),
    password_confirm: str = Form(),
):
    account_enabled()
    rate_limit(request)
    username, email = username.strip().lower(), email.strip().lower()
    cfg = get_settings().api
    error = "無法建立帳號。請確認資料，或使用既有登入方式。"
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,39}", username)
        or len(email) > 254
        or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
        or not valid_password(password)
        or password != password_confirm
    ):
        return render(request, "auth_register.html", error=error)
    try:
        with session_scope() as db:
            owner = db.scalar(select(AccountUser).where(AccountUser.owner_slot == "owner"))
            if (
                username == cfg.owner_username.strip().lower()
                or email == cfg.owner_email.strip().lower()
                or (owner and (username == owner.username or email == owner.email))
            ):
                return render(request, "auth_register.html", error=error)
            user = AccountUser(
                username=username, email=email, password_hash=PASSWORDS.hash(password)
            )
            db.add(user)
            db.flush()
            token = new_session(db, user, request)
    except IntegrityError:
        return render(request, "auth_register.html", error=error)
    return session_response(token, "/account")


@router.get("/account")
def account_page(request: Request, error: str | None = None, notice: str | None = None):
    account_enabled()
    user = current_user(request)
    with session_scope() as db:
        row = db.get(AccountUser, user["id"])
        sessions = [
            {
                "id": s.id,
                "user_agent": s.user_agent,
                "last_seen_at": aware(s.last_seen_at).isoformat(),
                "expires_at": aware(s.expires_at).isoformat(),
                "current": s.id == request.state.browser_session_id,
            }
            for s in db.scalars(
                select(BrowserSession)
                .where(BrowserSession.user_id == row.id, BrowserSession.expires_at > now())
                .order_by(BrowserSession.last_seen_at.desc())
            )
        ]
        linked = bool(db.scalar(select(OAuthIdentity.id).where(OAuthIdentity.user_id == row.id)))
        has_password = bool(row.password_hash)
    return render(
        request,
        "account.html",
        account_user=user,
        sessions=sessions,
        error="操作未完成，請確認密碼或重新登入後再試。" if error else None,
        notice="設定已更新。" if notice else None,
        google_linked=linked,
        google_enabled=google_enabled(),
        has_password=has_password,
    )


@router.post("/account/password")
def change_password(
    request: Request,
    password: str = Form(),
    password_confirm: str = Form(),
    current_password: str = Form(""),
):
    account_enabled()
    user = current_user(request)
    rate_limit(request)
    with session_scope() as db:
        row = db.get(AccountUser, user["id"])
        password_verified = (
            verify_password(current_password, row.password_hash) if row.password_hash else False
        )
        google_recent = recent(request) and request.state.account_session.auth_method == "google"
        allowed = password_verified or google_recent
        if not allowed or not valid_password(password) or password != password_confirm:
            return RedirectResponse("/account?error=password", 303)
        row.password_hash = PASSWORDS.hash(password)
        db.execute(delete(BrowserSession).where(BrowserSession.user_id == row.id))
        db.add(AccountAuditEvent(actor_id=row.id, user_id=row.id, action="password_changed"))
        token = new_session(
            db,
            row,
            request,
            auth_method="password" if password_verified else "google",
            authenticated_at=now()
            if password_verified
            else request.state.account_session.authenticated_at,
        )
    return session_response(token, "/account?notice=password")


@router.post("/account/sessions/{session_id}/revoke")
def revoke_session(session_id: int, request: Request):
    user = current_user(request)
    with session_scope() as db:
        row = db.get(BrowserSession, session_id)
        if not row or row.user_id != user["id"]:
            raise HTTPException(404)
        db.delete(row)
    return RedirectResponse("/account?notice=session", 303)


@router.get("/admin/users")
def users_page(request: Request, error: str | None = None, notice: str | None = None):
    user = current_user(request)
    if user["role"] not in {"owner", "admin"} or user["status"] != "active":
        raise HTTPException(403)
    with session_scope() as db:
        users = [
            user_dict(row)
            for row in db.scalars(select(AccountUser).order_by(AccountUser.created_at))
        ]
    return render(
        request,
        "account_users.html",
        account_user=user,
        users=users,
        error="無法變更此帳號的權限。" if error else None,
        notice="帳號權限已更新。" if notice else None,
    )


@router.post("/admin/users/{user_id}")
def update_user(user_id: int, request: Request, role: str = Form(), status: str = Form()):
    actor = current_user(request)
    if actor["role"] not in {"owner", "admin"} or actor["status"] != "active":
        raise HTTPException(403)
    with session_scope() as db:
        target = db.get(AccountUser, user_id)
        if (
            not target
            or target.role == "owner"
            or role not in {"admin", "viewer"}
            or status not in {"pending", "active", "disabled"}
            or (actor["role"] != "owner" and (target.role != "viewer" or role != "viewer"))
        ):
            return RedirectResponse("/admin/users?error=permission", 303)
        changes = {
            "before": {"role": target.role, "status": target.status},
            "after": {"role": role, "status": status},
        }
        target.role, target.status = role, status
        db.execute(delete(BrowserSession).where(BrowserSession.user_id == target.id))
        db.add(
            AccountAuditEvent(
                actor_id=actor["id"],
                user_id=target.id,
                action="permissions_changed",
                changes=changes,
            )
        )
    return RedirectResponse("/admin/users?notice=updated", 303)


@router.get("/auth/google")
async def google_start(request: Request, link: bool = False, next: str = "/"):
    account_enabled()
    rate_limit(request)
    if not google_enabled():
        return RedirectResponse("/login?error=google_unavailable", 303)
    user = current_user(request) if link else None
    if link and not recent(request):
        return RedirectResponse("/account?error=reauthenticate", 303)
    state, binding, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
    cfg = get_settings().api
    async with AsyncOAuth2Client(
        cfg.google_client_id,
        cfg.google_client_secret,
        redirect_uri=cfg.google_redirect_uri,
        scope="openid email profile",
        code_challenge_method="S256",
    ) as client:
        url, _ = client.create_authorization_url(
            "https://accounts.google.com/o/oauth2/v2/auth",
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            prompt="select_account",
        )
    with session_scope() as db:
        db.execute(delete(AuthTransaction).where(AuthTransaction.expires_at <= now()))
        db.add(
            AuthTransaction(
                state_hash=digest(state),
                binding_hash=digest(binding + ":" + request.cookies[COOKIE])
                if user
                else digest(binding),
                nonce=nonce,
                verifier=verifier,
                user_id=user["id"] if user else None,
                session_id=request.state.browser_session_id if user else None,
                next_path=safe_next(next),
                expires_at=now() + timedelta(minutes=10),
            )
        )
    response = RedirectResponse(url, 303)
    response.set_cookie(
        OAUTH_COOKIE,
        binding,
        max_age=600,
        httponly=True,
        secure=cfg.session_cookie_secure,
        samesite="lax",
        path="/auth/google",
    )
    return response


async def exchange_google(code, transaction):
    """Only fixed Google endpoints; verify signed OIDC claims, never trust userinfo alone."""
    cfg = get_settings().api
    async with AsyncOAuth2Client(
        cfg.google_client_id,
        cfg.google_client_secret,
        redirect_uri=cfg.google_redirect_uri,
        token_endpoint_auth_method="client_secret_post",
        timeout=15,
    ) as client:
        token = await client.fetch_token(
            "https://oauth2.googleapis.com/token",
            code=code,
            code_verifier=transaction.verifier,
            grant_type="authorization_code",
        )
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get("https://www.googleapis.com/oauth2/v3/certs")
        response.raise_for_status()
        jwks = response.json()
    claims = JsonWebToken(["RS256"]).decode(
        token["id_token"],
        jwks,
        claims_cls=CodeIDToken,
        claims_options={
            "iss": {
                "essential": True,
                "values": ["https://accounts.google.com", "accounts.google.com"],
            },
            "aud": {"essential": True, "value": cfg.google_client_id},
            "exp": {"essential": True},
            "sub": {"essential": True},
        },
        claims_params={
            "nonce": transaction.nonce,
            "client_id": cfg.google_client_id,
            "access_token": token.get("access_token"),
        },
    )
    claims.validate(leeway=30)
    if (
        claims.get("email_verified") is not True
        or not claims.get("email")
        or claims.get("nonce") != transaction.nonce
        or not claims.get("sub")
    ):
        raise ValueError("Unverified Google identity")
    return dict(claims)


@router.get("/auth/google/callback")
async def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    account_enabled()
    fail = RedirectResponse("/login?error=google", 303)
    fail.delete_cookie(OAUTH_COOKIE, path="/auth/google")
    if (
        not google_enabled()
        or not state
        or len(state) > 256
        or not request.cookies.get(OAUTH_COOKIE)
    ):
        return fail
    # DELETE RETURNING atomically consumes state across concurrent callbacks/processes.
    with session_scope() as db:
        transaction = db.scalar(
            delete(AuthTransaction)
            .where(
                AuthTransaction.state_hash == digest(state),
                or_(
                    and_(
                        AuthTransaction.user_id.is_(None),
                        AuthTransaction.binding_hash == digest(request.cookies[OAUTH_COOKIE]),
                    ),
                    and_(
                        AuthTransaction.user_id.is_not(None),
                        AuthTransaction.binding_hash
                        == digest(
                            request.cookies[OAUTH_COOKIE] + ":" + request.cookies.get(COOKIE, "")
                        ),
                    ),
                ),
                AuthTransaction.expires_at > now(),
            )
            .returning(AuthTransaction)
        )
    if not transaction or error or not code or len(code) > 4096:
        return fail
    try:
        claims = await exchange_google(code, transaction)
        email = claims["email"].strip().lower()
        with session_scope() as db:
            # Serialize identity/session publication with account changes. No DB lock
            # is held across the network exchange. Changes committed while awaiting
            # Google must be observed before any new identity or session is created.
            if db.bind.dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            identity = db.scalar(
                select(OAuthIdentity).where(
                    OAuthIdentity.provider == "google", OAuthIdentity.subject == claims["sub"]
                )
            )
            if transaction.user_id:
                bound_session = db.scalar(
                    select(BrowserSession)
                    .where(
                        BrowserSession.id == transaction.session_id,
                        BrowserSession.user_id == transaction.user_id,
                        BrowserSession.token_hash == digest(request.cookies.get(COOKIE, "")),
                        BrowserSession.expires_at > now(),
                        BrowserSession.authenticated_at > now() - timedelta(minutes=10),
                    )
                    .with_for_update()
                )
                user = db.scalar(
                    select(AccountUser)
                    .where(AccountUser.id == transaction.user_id)
                    .with_for_update()
                )
                if (
                    not bound_session
                    or not user
                    or user.status == "disabled"
                    or user.email != email
                    or not google_email_authoritative(claims)
                    or (identity and identity.user_id != user.id)
                ):
                    return fail
                if not identity:
                    db.add(OAuthIdentity(user_id=user.id, subject=claims["sub"]))
            elif identity:
                user = db.get(AccountUser, identity.user_id)
            else:
                if db.scalar(select(AccountUser.id).where(AccountUser.email == email)):
                    return RedirectResponse("/login?error=link_required", 303)
                cfg = get_settings().api
                owner_exists = db.scalar(
                    select(AccountUser.id).where(AccountUser.owner_slot == "owner")
                )
                owner = bool(
                    cfg.owner_email
                    and email == cfg.owner_email.strip().lower()
                    and not owner_exists
                )
                if owner and not google_email_authoritative(claims):
                    return fail
                username = (
                    cfg.owner_username.strip().lower() if owner else "user-" + secrets.token_hex(6)
                )
                user = AccountUser(
                    username=username,
                    email=email,
                    role="owner" if owner else "viewer",
                    status="active" if owner else "pending",
                    owner_slot="owner" if owner else None,
                )
                db.add(user)
                db.flush()
                db.add(OAuthIdentity(user_id=user.id, subject=claims["sub"]))
            if not user or user.status == "disabled":
                return fail
            token = new_session(db, user, request, auth_method="google")
            destination = (
                "/account?notice=linked"
                if transaction.user_id
                else transaction.next_path
                if user.status == "active"
                else "/account"
            )
        response = session_response(token, destination)
        response.delete_cookie(OAUTH_COOKIE, path="/auth/google")
        return response
    except Exception:
        # OIDC/provider exceptions can contain codes or token response bodies.
        return fail


def recover_account_password(password, username=None):
    """Local operator recovery only; owner must already exist via verified Google bootstrap."""
    if not valid_password(password):
        raise ValueError("密碼長度必須為 12 至 256 字元。")
    with session_scope() as db:
        owner = db.scalar(
            select(AccountUser).where(
                AccountUser.username == username.strip().lower()
                if username
                else AccountUser.owner_slot == "owner"
            )
        )
        if not owner:
            raise ValueError(
                "找不到帳號。" if username else "請先使用已設定的 Google 帳號建立擁有者。"
            )
        owner.password_hash = PASSWORDS.hash(password)
        db.execute(delete(BrowserSession).where(BrowserSession.user_id == owner.id))
        db.add(AccountAuditEvent(user_id=owner.id, action="operator_password_recovery"))


def recover_owner_password(password):
    return recover_account_password(password)
