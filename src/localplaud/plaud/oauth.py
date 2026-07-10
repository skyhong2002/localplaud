"""OAuth token management for the official Plaud Open API.

The one-time browser login happens through the official Plaud CLI
(``plaud login`` / ``npx -y @plaud-ai/cli``), which writes a token set to
``~/.plaud/tokens.json``. localplaud reads that file and keeps the session
alive by refreshing the access token before it expires (or on a 401), writing
the refreshed set back so the official CLI and localplaud share one session.

Token file shape (managed by the official CLI — do not hand-edit):
``{"access_token", "refresh_token", "token_type", "expires_at"}`` with
``expires_at`` in epoch **milliseconds**.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# Refresh this long before the recorded expiry, matching the official CLI.
_EXPIRY_SLACK_MS = 60_000


class OAuthError(RuntimeError):
    pass


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
        self._tokens = tokens
        self.tokens_path.parent.mkdir(parents=True, exist_ok=True)
        self.tokens_path.write_text(json.dumps(tokens, indent=2))

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
