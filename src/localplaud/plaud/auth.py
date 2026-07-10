"""Plaud authentication.

Plaud's web app authenticates to the API with header-token auth — an
``Authorization`` value plus a set of client/device headers — not a single
readable cookie (see AGENTS.md / docs/plaud-api.md). Until the programmatic
login flow is reverse-engineered, the supported route is: the user copies an
authenticated request's headers out of their browser and localplaud replays
them.

To make that painless, ``parse_curl`` turns a DevTools "Copy as cURL" string
into the header set, and ``build_client`` assembles the httpx client.
"""

from __future__ import annotations

import logging
import re
import shlex

import httpx

from ..config import PlaudConfig

log = logging.getLogger(__name__)

# Sensible defaults for the Plaud client headers. The API sits behind an edge
# that rejects non-browser-looking requests with 403, so we send the same
# Origin/Referer/Accept the web app does. Any of these can be overridden via
# ``plaud.extra_headers`` in config.
_DEFAULT_CLIENT_HEADERS = {
    "app-platform": "web",
    "app-language": "en",
    "edit-from": "web",
    "Origin": "https://web.plaud.ai",
    "Referer": "https://web.plaud.ai/",
    "Accept": "application/json, text/plain, */*",
}


class AuthError(RuntimeError):
    pass


def _normalize_authorization(value: str) -> str:
    """Accept a bare token or a full ``Bearer ...`` value."""
    v = value.strip()
    if not v:
        return v
    if " " in v:  # already "Bearer xxx" / "Token xxx"
        return v
    return f"Bearer {v}"


def build_headers(cfg: PlaudConfig) -> dict[str, str]:
    """Assemble the request headers for the Plaud API from config."""
    headers: dict[str, str] = dict(_DEFAULT_CLIENT_HEADERS)
    headers["User-Agent"] = cfg.user_agent

    if cfg.token:
        headers["Authorization"] = _normalize_authorization(cfg.token)
    if cfg.cookie:
        # ``cookie`` may hold either a Cookie header value or a full pasted
        # Authorization value — be forgiving.
        c = cfg.cookie.strip()
        if c.lower().startswith("authorization:"):
            headers["Authorization"] = c.split(":", 1)[1].strip()
        elif c.lower().startswith(("bearer ", "token ")):
            headers["Authorization"] = c
        else:
            headers["Cookie"] = c

    # User-supplied headers win (x-device-id, x-pld-user, app-version, ...).
    for k, v in cfg.extra_headers.items():
        headers[k] = v

    if "Authorization" not in headers and "Cookie" not in headers:
        raise AuthError(
            "No Plaud credentials configured. Set plaud.token (Authorization), "
            "plaud.cookie, or plaud.extra_headers — see README 'Your Plaud "
            "session'. Tip: `localplaud auth import` parses a browser cURL."
        )
    return headers


def build_client(cfg: PlaudConfig) -> httpx.Client:
    """An httpx client pinned to the account's API base with auth headers."""
    return httpx.Client(
        base_url=cfg.api_base.rstrip("/"),
        headers=build_headers(cfg),
        timeout=cfg.request_timeout_seconds,
        follow_redirects=True,
    )


# --------------------------------------------------------------------------- #
# cURL import helper — turns DevTools "Copy as cURL" into config values.
# --------------------------------------------------------------------------- #


def parse_curl(curl_text: str) -> dict[str, object]:
    """Extract ``url``, ``headers`` and ``cookies`` from a cURL command.

    Handles the ``curl 'url' -H 'K: V' -H ... -b 'cookie'`` form that both
    Chrome and Firefox produce. Returns a dict suitable for populating
    ``[plaud]`` config.
    """
    # Join line continuations.
    text = curl_text.replace("\\\n", " ").strip()
    try:
        tokens = shlex.split(text)
    except ValueError:
        # Fall back to a looser regex if quoting is malformed.
        tokens = re.findall(r"'[^']*'|\"[^\"]*\"|\S+", text)
        tokens = [t.strip("'\"") for t in tokens]

    url: str | None = None
    headers: dict[str, str] = {}
    cookie: str | None = None

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-H", "--header") and i + 1 < len(tokens):
            k, _, v = tokens[i + 1].partition(":")
            headers[k.strip()] = v.strip()
            i += 2
            continue
        if tok in ("-b", "--cookie") and i + 1 < len(tokens):
            cookie = tokens[i + 1].strip()
            i += 2
            continue
        if tok in ("-A", "--user-agent") and i + 1 < len(tokens):
            headers["User-Agent"] = tokens[i + 1].strip()
            i += 2
            continue
        if tok.startswith("http://") or tok.startswith("https://"):
            url = tok
        elif tok == "curl" or tok.startswith("-"):
            pass
        elif url is None and "plaud" in tok:
            url = tok
        i += 1

    # Pull the API base (scheme+host) out of the URL if present.
    api_base = None
    if url:
        m = re.match(r"(https?://[^/]+)", url)
        if m:
            api_base = m.group(1)

    # Header names are case-insensitive and browsers emit them lowercased in
    # "Copy as cURL" — match accordingly and pull auth/cookie out of the set.
    def pop_ci(name: str) -> str | None:
        for k in list(headers):
            if k.lower() == name.lower():
                return headers.pop(k)
        return None

    result: dict[str, object] = {}
    if api_base:
        result["api_base"] = api_base
    token = pop_ci("authorization")
    if token:
        result["token"] = token
    header_cookie = pop_ci("cookie")
    cookie = cookie or header_cookie
    if cookie:
        result["cookie"] = cookie
    # Drop browser-noise headers; keep the Plaud client/device headers.
    for noise in ("origin", "referer", "host", "accept", "accept-encoding",
                  "accept-language", "sec-fetch-mode", "sec-fetch-site",
                  "sec-fetch-dest", "user-agent", "content-length", "connection"):
        pop_ci(noise)
    result["headers"] = headers
    return result
