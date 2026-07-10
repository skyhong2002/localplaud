"""Plaud cloud clients (read-only).

Two interchangeable providers, selected by ``plaud.provider``:

- :class:`~localplaud.plaud.official.PlaudOfficialClient` — the official Open
  API with auto-refreshing OAuth (default).
- :class:`~localplaud.plaud.client.PlaudClient` — the reverse-engineered
  api-apse1 web API, driven by a pasted browser session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import PlaudConfig

if TYPE_CHECKING:
    from .client import PlaudClient
    from .official import PlaudOfficialClient


def make_plaud_client(cfg: PlaudConfig) -> PlaudClient | PlaudOfficialClient:
    """Build the configured Plaud client (imports lazily to keep CLI startup
    fast)."""
    if cfg.provider == "official":
        from .official import PlaudOfficialClient

        return PlaudOfficialClient(cfg.official)
    from .client import PlaudClient

    return PlaudClient(cfg)
