"""HTTP cache policy for the shell's public assets.

Everything the Web App serves is ``private, no-store`` (see ``_auth_gate`` in
``app.py``) except two kinds of non-user-specific assets, which set their own
policy and keep it in every auth mode:

* ``/static/*`` (public in every mode): a URL whose ``v`` query equals the
  file's own content hash is immutable for a year; any other request
  revalidates with the ETag, so an upgrade never pins stale code.
* ``/i18n/<locale>.js`` (behind the session gate): the versioned catalog is
  ``private`` and immutable; any other version revalidates.

Templates build versioned static URLs with :func:`static_url`, so the hash in
the URL is exactly the one the server checks.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"
ONE_YEAR = 31_536_000
PUBLIC_IMMUTABLE = f"public, max-age={ONE_YEAR}, immutable"
PRIVATE_IMMUTABLE = f"private, max-age={ONE_YEAR}, immutable"
REVALIDATE = "no-cache"
PRIVATE_REVALIDATE = "private, no-cache"
NO_STORE = "private, no-store"

_OWN_POLICY_PREFIXES = ("/static/", "/i18n/")


@lru_cache(maxsize=1024)
def _digest(path: str, mtime_ns: int, size: int) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def _file_version(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return _digest(str(path), stat.st_mtime_ns, stat.st_size)


def static_url(name: str) -> str:
    """``/static/<name>?v=<content hash>`` for a file under the static folder."""
    version = _file_version(STATIC_DIR / name)
    return f"/static/{name}?v={version}" if version else f"/static/{name}"


def keeps_own_cache_policy(path: str, response: Response) -> bool:
    """Whether a successful asset response may keep the caching its route chose.

    Login redirects, 401/403 rejections and errors never qualify, so they stay
    ``no-store`` like every other response.
    """
    return (
        path.startswith(_OWN_POLICY_PREFIXES)
        and response.status_code in {200, 304}
        and "cache-control" in response.headers
    )


class StaticAssets(StaticFiles):
    """Static files with content-hash cache headers instead of none at all."""

    def file_response(self, full_path, stat_result, scope, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        requested = Request(scope).query_params.get("v")
        current = _digest(str(full_path), stat_result.st_mtime_ns, stat_result.st_size)
        immutable = bool(requested) and requested == current
        response.headers["Cache-Control"] = PUBLIC_IMMUTABLE if immutable else REVALIDATE
        return response
