"""Native PKCE OAuth and token management for the official Plaud Open API.

The loopback login is protocol-compatible with the official Plaud CLI and shares
its ``~/.plaud/tokens.json`` cache, but requires no Node.js installation.

Token file shape (compatible with the official CLI — do not hand-edit):
``{"access_token", "refresh_token", "token_type", "expires_at"}`` with
``expires_at`` in epoch **milliseconds**.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

log = logging.getLogger(__name__)

# Refresh this long before the recorded expiry, matching the official CLI.
_EXPIRY_SLACK_MS = 60_000


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    code_verifier: str
    state: str


class OfficialTokenStore:
    """Read/refresh/persist the official CLI's token set.

    Thread-safe: the poller downloads concurrently through one client, so the
    refresh path is serialized — only one thread hits the refresh endpoint,
    the rest reuse its result.
    """

    def __init__(self, tokens_path: Path, refresh_url: str, timeout: float = 30.0):
        self.tokens_path = tokens_path.expanduser()
        self.refresh_url = refresh_url
        self.timeout = timeout
        self._lock = threading.Lock()
        self._tokens: dict | None = None

    # ---- persistence ----------------------------------------------------- #

    def _load(self) -> dict | None:
        if self._tokens is not None:
            return self._tokens
        try:
            self._tokens = json.loads(self.tokens_path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise OAuthError(f"cannot read {self.tokens_path}: {exc}") from exc
        return self._tokens

    def _save(self, tokens: dict) -> None:
        self.tokens_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.tokens_path.with_name(f".{self.tokens_path.name}.tmp")
        temporary.write_text(json.dumps(tokens, indent=2))
        temporary.chmod(0o600)
        temporary.replace(self.tokens_path)
        self._tokens = tokens

    # ---- access ----------------------------------------------------------- #

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Return a valid access token, refreshing it when it is (about to be)
        expired. Raises :class:`OAuthError` when there is no usable session."""
        with self._lock:
            tokens = self._load()
            if not tokens or not tokens.get("access_token"):
                raise OAuthError(
                    f"no Plaud OAuth session at {self.tokens_path} — run "
                    "`localplaud auth login` (or `plaud login`) once to sign in."
                )
            expires_at = tokens.get("expires_at")
            stale = force_refresh or (
                isinstance(expires_at, (int, float))
                and time.time() * 1000 > expires_at - _EXPIRY_SLACK_MS
            )
            if stale:
                tokens = self._refresh_locked(tokens)
            return tokens["access_token"]

    def _refresh_locked(self, tokens: dict) -> dict:
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise OAuthError(
                "Plaud access token expired and no refresh token is stored — "
                "run `localplaud auth login` again."
            )
        resp = httpx.post(
            self.refresh_url,
            data={"refresh_token": refresh_token},
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise OAuthError(
                f"Plaud token refresh failed ({resp.status_code}): {resp.text[:200]} "
                "— run `localplaud auth login` to sign in again."
            )
        data = resp.json()
        if not data.get("access_token"):
            raise OAuthError("Plaud token refresh returned no access_token")
        new_tokens = {
            "access_token": data["access_token"],
            # The refresh endpoint may not rotate the refresh token — keep the
            # old one in that case (matches the official CLI).
            "refresh_token": data.get("refresh_token") or refresh_token,
            "token_type": data.get("token_type", tokens.get("token_type", "bearer")),
            "expires_at": (
                int(time.time() * 1000 + data["expires_in"] * 1000)
                if data.get("expires_in")
                else None
            ),
        }
        self._save(new_tokens)
        log.info("Refreshed Plaud OAuth access token (expires_at=%s)", new_tokens["expires_at"])
        return new_tokens

    def status(self) -> dict:
        """Non-raising introspection for `doctor`/`auth check` UX."""
        try:
            tokens = self._load()
        except OAuthError as exc:
            return {"ok": False, "detail": str(exc)}
        if not tokens or not tokens.get("access_token"):
            return {"ok": False, "detail": f"no session at {self.tokens_path}"}
        expires_at = tokens.get("expires_at")
        expired = isinstance(expires_at, (int, float)) and time.time() * 1000 > expires_at
        if expired and not tokens.get("refresh_token"):
            return {"ok": False, "detail": "expired, no refresh token"}
        return {"ok": True, "detail": "expired, will auto-refresh" if expired else "valid"}


def create_authorization_request(
    authorization_url: str, client_id: str, redirect_uri: str
) -> AuthorizationRequest:
    code_verifier = secrets.token_urlsafe(32)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return AuthorizationRequest(
        url=f"{authorization_url}?{query}", code_verifier=code_verifier, state=state
    )


def exchange_authorization_code(
    *,
    token_url: str,
    client_id: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    state: str,
    store: OfficialTokenStore,
    timeout: float = 30.0,
) -> dict:
    basic = base64.b64encode(f"{client_id}:".encode()).decode()
    response = httpx.post(
        token_url,
        data={
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "state": state,
        },
        headers={
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise OAuthError(
            f"Plaud token exchange failed ({response.status_code}): {response.text[:200]}"
        )
    data = response.json()
    if not data.get("access_token"):
        raise OAuthError("Plaud token exchange returned no access_token")
    if not data.get("refresh_token"):
        raise OAuthError("Plaud token exchange returned no refresh_token")
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "token_type": data.get("token_type", "Bearer"),
        "expires_at": (
            int(time.time() * 1000 + data["expires_in"] * 1000)
            if data.get("expires_in")
            else None
        ),
    }
    store._save(tokens)
    return tokens


def run_loopback_callback(
    *,
    expected_state: str,
    exchange_code,
    host: str = "127.0.0.1",
    port: int = 8199,
    timeout: float = 120.0,
    on_listening=None,
) -> None:
    """Wait for one valid OAuth callback, ignoring unrelated/wrong-state traffic."""
    result: dict[str, object] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, _format, *args):
            return

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler contract
            parsed = urlparse(self.path)
            if parsed.path != "/auth/callback":
                self.send_error(404)
                return
            params = parse_qs(parsed.query)
            state = (params.get("state") or [None])[0]
            if state != expected_state:
                self._html(200, "Continue authorization in the original window.")
                return
            if params.get("error"):
                detail = (params.get("error_description") or params["error"])[0]
                result["error"] = OAuthError(f"Plaud authorization denied: {detail}")
                self._html(400, f"Authorization denied: {detail}")
                return
            code = (params.get("code") or [None])[0]
            if not code:
                self._html(200, "Continue authorization in the original window.")
                return
            try:
                exchange_code(code)
                result["success"] = True
                self._html(200, "Authorization successful. You can close this tab.")
            except Exception as exc:  # noqa: BLE001 - surfaced as OAuthError below
                result["error"] = exc
                self._html(500, f"Authorization failed: {exc}")

        def _html(self, status: int, message: str):
            body = (
                "<!doctype html><meta charset=utf-8><title>localplaud</title>"
                f"<body style='font-family:system-ui;padding:2rem;text-align:center'>"
                f"<h1>{html.escape(message)}</h1></body>"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = HTTPServer((host, port), CallbackHandler)
    except OSError as exc:
        raise OAuthError(
            f"cannot bind OAuth callback at {host}:{port}: {exc}. "
            f"Stop the process using port {port} and retry."
        ) from exc
    deadline = time.monotonic() + timeout
    server.timeout = min(0.5, timeout)
    try:
        if on_listening:
            on_listening()
        while time.monotonic() < deadline and not result:
            server.handle_request()
    finally:
        server.server_close()
    if error := result.get("error"):
        if isinstance(error, OAuthError):
            raise error
        if isinstance(error, BaseException):
            raise OAuthError(f"Plaud authorization failed: {error}") from error
        raise OAuthError(f"Plaud authorization failed: {error}")
    if not result.get("success"):
        raise OAuthError(f"Plaud authorization timed out after {int(timeout)} seconds")


def native_login(config, *, open_browser=webbrowser.open, show_manual_url=None) -> Path:
    """Run the official loopback PKCE login and return the written token path."""
    request = create_authorization_request(
        config.authorization_url, config.client_id, config.redirect_uri
    )
    redirect = urlparse(config.redirect_uri)
    if redirect.hostname not in {"localhost", "127.0.0.1"} or not redirect.port:
        raise OAuthError("Plaud OAuth redirect_uri must use a localhost port")
    store = OfficialTokenStore(
        config.tokens_path, config.refresh_url, config.request_timeout_seconds
    )

    def exchange(code: str):
        exchange_authorization_code(
            token_url=config.token_url,
            client_id=config.client_id,
            redirect_uri=config.redirect_uri,
            code=code,
            code_verifier=request.code_verifier,
            state=request.state,
            store=store,
            timeout=config.request_timeout_seconds,
        )

    def launch():
        try:
            opened = open_browser(request.url)
        except Exception:  # noqa: BLE001 - always offer the manual URL fallback
            opened = False
        if not opened:
            if show_manual_url:
                show_manual_url(request.url)
            else:
                log.warning("Could not open browser; authorization URL: %s", request.url)

    run_loopback_callback(
        expected_state=request.state,
        exchange_code=exchange,
        host="127.0.0.1",
        port=redirect.port,
        timeout=config.login_timeout_seconds,
        on_listening=launch,
    )
    return config.tokens_path.expanduser()
